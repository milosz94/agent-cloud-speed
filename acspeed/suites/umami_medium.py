"""Medium tier, instance #1: umami (analytics app + its datastore) plus a second site wired into it.

This is ONE instance of the Medium operation graph, not the graph itself. The engine
(``acspeed/suite.py``) knows nothing about umami; everything umami-specific lives in the verify
predicates below, which the RUNNER owns. The agent owns the METHOD for every mutation.

The operation graph (Medium):

  1. deploy-serve   (PROVISION)       umami serves and identifies as umami.
  2. mutate:register(OPERATE_MUTATE)  a known probe ADMIN user exists (durable identity state).
  3. integrate      (OPERATE_MUTATE)  a second site's traffic is recorded in umami (durable event state).
  4. restart        (OPERATE_MUTATE)  the services are restarted and umami serves again (liveness only).

Success = every DURABLE postcondition re-verified at the terminal state, i.e. AFTER the restart:
umami still serves, the probe user still logs in, and the recorded pageview is still counted. That
terminal conjunction is CP7 (restart-durability): the state survived a restart.

Why the verify predicates only touch umami's STABLE read surface: because the agent owns the mutate
method, the runner never needs umami's create-user or create-website API. It needs only
``POST /api/auth/login`` (stable across umami v2) and the website stats read. How the user, website
and pageview got created is the agent's business, on whatever surface it chose.

VALIDATE-LIVE: the exact stats endpoint shape (query params, the ``pageviews.value`` path) is umami's
and can shift between minor versions. These predicates are written against umami v2 and MUST be
validated against a live umami before the instance is trusted (a verify that cannot fail is not a
control). The login and serves checks are the load-bearing durability sentinels; the stats read has a
documented best-effort fallback so a shape drift degrades to "could not confirm", never a false pass.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ..suite import OpContext, TierInstance, TierOperation, VerifyResult
from ..operation import OPERATE_MUTATE, PROVISION
from ._http import http


def _make_probe() -> dict:
    """A per-run sentinel: a unique admin username + password, a website name and a URL path. Unique
    so re-runs never collide, and so the runner knows the exact thing to look for."""
    suffix = os.urandom(4).hex()
    return {
        "username": f"acspeed_probe_{suffix}",
        "password": f"Acspeed-{suffix}-pw",
        "website_name": f"acspeed-site-{suffix}",
        "sentinel_path": f"/acspeed-sentinel-{suffix}",
    }


# ---- the observe primitive, umami flavour (runner-owned; never consults the agent) -------------

def _serves_as_umami(url: str) -> VerifyResult:
    code, text, _ = http("GET", url, timeout_s=20)
    if code is None:
        return VerifyResult(False, "root did not answer")
    low = text.lower()
    is_umami = "umami" in low
    ok = code < 500 and is_umami
    return VerifyResult(
        ok,
        f"http {code}, umami-signature={'yes' if is_umami else 'no'}",
        measured={"http_code": code, "umami_signature": is_umami},
    )


def _login(url: str, username: str, password: str) -> Optional[str]:
    """POST /api/auth/login; return the bearer token, or None. Stable across umami v2."""
    code, _, parsed = http(
        "POST", url.rstrip("/") + "/api/auth/login",
        body={"username": username, "password": password}, timeout_s=20,
    )
    if code == 200 and isinstance(parsed, dict):
        tok = parsed.get("token")
        if isinstance(tok, str) and tok:
            return tok
    return None


def _probe_login(ctx: OpContext) -> VerifyResult:
    probe = ctx.state["probe"]
    if not ctx.url:
        return VerifyResult(False, "no served url")
    tok = _login(ctx.url, probe["username"], probe["password"])
    if tok:
        ctx.state["probe_token"] = tok  # reused by the integrate read (probe is an admin)
        return VerifyResult(True, f"probe user {probe['username']} authenticates", measured={"token": bool(tok)})
    return VerifyResult(False, f"probe user {probe['username']} could not authenticate")


def _list_websites(url: str, token: str) -> list:
    """GET the websites list. umami has returned either a bare list or ``{data: [...]}`` across
    versions; tolerate both."""
    code, _, parsed = http("GET", url.rstrip("/") + "/api/websites", bearer=token, timeout_s=20)
    if code != 200:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
        return parsed["data"]
    return []


def _pageviews_ge_1(url: str, token: str, website_id: str,
                    sentinel_path: Optional[str] = None) -> Optional[bool]:
    """True if the website has >=1 recorded pageview (preferably for `sentinel_path`). False if a read
    parsed but found none. None if NEITHER read could be parsed (best-effort: a shape drift is
    'unconfirmed', never a false pass). Two reads: umami's /stats (`pageviews.value`), then the
    /metrics?type=path breakdown, which the reference run confirmed returns [{"x": path, "y": count}]
    and is the precise check for the exact sentinel path."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 90 * 24 * 3600 * 1000  # 90 days back: wide enough for any run
    end_ms = now_ms + 24 * 3600 * 1000
    base = f"{url.rstrip('/')}/api/websites/{website_id}"
    saw_shape = False
    # 1. /stats: an aggregate pageviews count
    code, _, parsed = http("GET", f"{base}/stats?startAt={start_ms}&endAt={end_ms}",
                           bearer=token, timeout_s=20)
    if code == 200 and isinstance(parsed, dict) and isinstance(parsed.get("pageviews"), (dict, int, float)):
        pv = parsed["pageviews"]
        val = pv.get("value") if isinstance(pv, dict) else pv
        try:
            if float(val) >= 1:
                return True
            saw_shape = True
        except (TypeError, ValueError):
            pass
    # 2. /metrics?type=path: the per-path breakdown (the reference-run endpoint); precise for the sentinel
    code, _, parsed = http("GET", f"{base}/metrics?type=path&startAt={start_ms}&endAt={end_ms}",
                           bearer=token, timeout_s=20)
    if code == 200 and isinstance(parsed, list):
        saw_shape = True
        for row in parsed:
            if not isinstance(row, dict):
                continue
            try:
                y = float(row.get("y", 0))
            except (TypeError, ValueError):
                y = 0
            if y >= 1 and (not sentinel_path or sentinel_path in str(row.get("x", ""))):
                return True
    return False if saw_shape else None


def _integration_recorded(ctx: OpContext) -> VerifyResult:
    probe = ctx.state["probe"]
    if not ctx.url:
        return VerifyResult(False, "no served url")
    token = ctx.state.get("probe_token") or _login(ctx.url, probe["username"], probe["password"])
    if not token:
        return VerifyResult(False, "could not authenticate to read stats")
    sites = _list_websites(ctx.url, token)
    if not sites:
        return VerifyResult(False, "no websites registered in umami")
    # Prefer the sentinel website by name/domain; otherwise fall back to any website that has traffic.
    def _match(s: dict) -> bool:
        blob = f"{s.get('name','')} {s.get('domain','')}".lower()
        return probe["website_name"].lower() in blob

    ordered = [s for s in sites if _match(s)] + [s for s in sites if not _match(s)]
    unconfirmed = False
    for s in ordered:
        wid = s.get("id") or s.get("websiteId")
        if not wid:
            continue
        res = _pageviews_ge_1(ctx.url, token, str(wid), sentinel_path=probe.get("sentinel_path"))
        if res is True:
            return VerifyResult(
                True, f"website {s.get('name', wid)} has >=1 recorded pageview",
                measured={"website_id": str(wid)},
            )
        if res is None:
            unconfirmed = True
    if unconfirmed:
        return VerifyResult(False, "stats endpoint returned an unrecognized shape (VALIDATE-LIVE)")
    return VerifyResult(False, "no website has a recorded pageview")


# ---- the tier instance -------------------------------------------------------------------------

def build(probe: Optional[dict] = None) -> TierInstance:
    """Build the umami Medium instance. ``probe`` is the per-run sentinel; a fresh one is generated
    if not supplied. The task texts embed the sentinel so the agent creates exactly what the runner
    will look for; the verify predicates read the same sentinel from ``ctx.state['probe']``."""
    probe = probe or _make_probe()

    def _seed_probe(ctx: OpContext) -> None:
        ctx.state.setdefault("probe", probe)

    def v_deploy(ctx: OpContext) -> VerifyResult:
        _seed_probe(ctx)
        return _serves_as_umami(ctx.url) if ctx.url else VerifyResult(False, "no served url")

    def v_register(ctx: OpContext) -> VerifyResult:
        _seed_probe(ctx)
        return _probe_login(ctx)

    def v_integrate(ctx: OpContext) -> VerifyResult:
        _seed_probe(ctx)
        return _integration_recorded(ctx)

    def v_restart(ctx: OpContext) -> VerifyResult:
        _seed_probe(ctx)
        return _serves_as_umami(ctx.url) if ctx.url else VerifyResult(False, "no served url")

    ops = [
        TierOperation(
            op_id="deploy-serve",
            op_type=PROVISION,
            task=(
                "Deploy the umami web analytics application together with its database so it is fully "
                "running and serving over HTTP. It must reach its login screen and be able to "
                "authenticate (its database must be connected, not just the web server up)."
            ),
            verify=v_deploy,
            durable=True,
        ),
        TierOperation(
            op_id="mutate:register",
            op_type=OPERATE_MUTATE,
            task=(
                f"In the running umami, create a new ADMIN user with username '{probe['username']}' "
                f"and password '{probe['password']}'. Use whatever method your deployment allows "
                "(the app UI, its API, or a direct database/admin path). The user must be able to log "
                "in afterwards."
            ),
            verify=v_register,
            depends_on=("deploy-serve",),
            durable=True,
        ),
        TierOperation(
            op_id="integrate",
            op_type=OPERATE_MUTATE,
            task=(
                f"Log in to umami as the '{probe['username']}' account you created in the previous step, "
                f"and under THAT account create a umami website named '{probe['website_name']}' (umami "
                "websites are owned per-user, so it must belong to that account, not the default admin). "
                "Deploy a second, simple web page (a static site is fine) and wire umami's tracking script "
                f"into it for that website. Then generate at least one real pageview through that tracking "
                f"for the path '{probe['sentinel_path']}', so the visit is recorded in umami under the "
                f"'{probe['username']}' account. The second site's traffic must show up inside umami, owned "
                "by that account."
            ),
            verify=v_integrate,
            depends_on=("deploy-serve", "mutate:register"),
            durable=True,
        ),
        TierOperation(
            op_id="restart",
            op_type=OPERATE_MUTATE,
            task=(
                "Restart the running services (both umami and its database) the way your chosen "
                "deployment supports it (restart the containers / instances / managed service). After "
                "the restart, umami must be serving again."
            ),
            verify=v_restart,
            depends_on=("deploy-serve",),
            durable=False,  # its only postcondition is liveness; durability is the terminal conjunction
        ),
    ]
    return TierInstance(
        name="umami-medium", tier="medium", operations=ops,
        teardown_hint=("the umami application, its managed database, and the second website you deployed "
                       "for the analytics integration"),
    )
