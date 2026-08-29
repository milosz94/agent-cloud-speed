"""Hard tier, instance #1: umami under a capacity + fault battery.

Extends the Medium graph (deploy -> register -> integrate -> restart-durability) with three operations
the spec calls for, and a scale-aware terminal conjunction:

  * harden   (OPERATE_MUTATE)  the agent upgrades the deployment toward production-grade -- an HA /
                               replicated / backed-up datastore, a restart or health policy, more than
                               one app instance -- using whatever its cloud offers. Verify: it still
                               serves (the real durability payoff is proven by surviving the fault below).
  * capacity (OPERATE_MUTATE)  BULK-LOAD >= capacity_target events through umami's OWN ingest path, so
                               durability and behavior are tested AT SCALE, not on one sentinel row.
                               Verify: the loaded website's recorded pageview count reaches the target.
  * fault    (OPERATE_MUTATE)  the agent injects a fault (restart / kill the datastore, force a failover,
                               and restart the app the cloud way). Verify: it serves again afterward.

The engine then re-verifies every DURABLE postcondition at the terminal state, i.e. AFTER the fault:
umami still serves, the probe admin still authenticates, the integration pageview is still recorded, and
the bulk-loaded >= capacity_target events are STILL there. That conjunction is the Hard tier's headline --
INTEGRITY-AT-SCALE UNDER FAULT: state survives a fault only if the hardening was real and the datastore
durable. Same contract as Medium: the agent owns the METHOD for every mutation, the runner owns
VERIFICATION (the observe primitive, read back independently), and success is the terminal conjunction.

capacity_target defaults to 10^4 for a tractable run; the published spec's full-study target is 10^5, and
it is a build() parameter, so a real study raises it. This instance reuses umami_medium's stable read
helpers (login / list-websites / serves), so it does not depend on umami's create surface -- how the
admin, website, bulk load, hardening and fault happen is the agent's business, on whatever surface it chose.
"""
from __future__ import annotations

import time
from typing import Optional

from . import umami_medium as um
from ._http import http
from ..operation import OPERATE_MUTATE, PROVISION
from ..suite import OpContext, TierInstance, TierOperation, VerifyResult


def _pageview_count(url: str, token: str, website_id: str) -> Optional[float]:
    """The website's recorded pageview COUNT over a wide window (umami /stats ``pageviews.value``, then the
    /metrics?type=path breakdown summed). None if neither read parses -- so a shape drift is 'unconfirmed',
    never a false pass (the capacity check then fails closed rather than declaring the load durable)."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 90 * 24 * 3600 * 1000
    end_ms = now_ms + 24 * 3600 * 1000
    base = f"{url.rstrip('/')}/api/websites/{website_id}"
    code, _, parsed = http("GET", f"{base}/stats?startAt={start_ms}&endAt={end_ms}", bearer=token, timeout_s=30)
    if code == 200 and isinstance(parsed, dict) and isinstance(parsed.get("pageviews"), (dict, int, float)):
        pv = parsed["pageviews"]
        val = pv.get("value") if isinstance(pv, dict) else pv
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    code, _, parsed = http("GET", f"{base}/metrics?type=path&startAt={start_ms}&endAt={end_ms}",
                           bearer=token, timeout_s=30)
    if code == 200 and isinstance(parsed, list):
        total = 0.0
        for row in parsed:
            if isinstance(row, dict):
                try:
                    total += float(row.get("y", 0) or 0)
                except (TypeError, ValueError):
                    pass
        return total
    return None


def _capacity_loaded(ctx: OpContext, threshold: int) -> VerifyResult:
    """The bulk-load landed at scale: the sentinel website has >= threshold recorded pageviews. Read as the
    probe admin via umami's own API (surface-independent), never from the agent's report."""
    probe = ctx.state["probe"]
    if not ctx.url:
        return VerifyResult(False, "no served url")
    token = ctx.state.get("probe_token") or um._login(ctx.url, probe["username"], probe["password"])
    if not token:
        return VerifyResult(False, "could not authenticate to read the capacity count")
    for s in um._list_websites(ctx.url, token):
        blob = f"{s.get('name', '')} {s.get('domain', '')}".lower()
        if probe["website_name"].lower() not in blob:
            continue
        wid = s.get("id") or s.get("websiteId")
        if not wid:
            continue
        n = _pageview_count(ctx.url, token, str(wid))
        if n is None:
            return VerifyResult(False, "could not read the pageview count (VALIDATE-LIVE)")
        return VerifyResult(n >= threshold,
                            f"website {s.get('name', wid)} has {int(n)} recorded pageviews (target {threshold})",
                            measured={"count": n, "target": threshold})
    return VerifyResult(False, "the capacity website was not found in umami")


def build(probe: Optional[dict] = None, capacity_target: int = 10000) -> TierInstance:
    """Build the umami Hard instance. ``probe`` is the per-run sentinel (a fresh one is generated if not
    given); ``capacity_target`` is the bulk-load size the agent must reach (default 10^4 for a tractable
    run; the spec's full-study target is 10^5)."""
    probe = probe or um._make_probe()

    def _seed(ctx: OpContext) -> None:
        ctx.state.setdefault("probe", probe)

    def v_deploy(ctx: OpContext) -> VerifyResult:
        _seed(ctx)
        return um._serves_as_umami(ctx.url) if ctx.url else VerifyResult(False, "no served url")

    def v_register(ctx: OpContext) -> VerifyResult:
        _seed(ctx)
        return um._probe_login(ctx)

    def v_integrate(ctx: OpContext) -> VerifyResult:
        _seed(ctx)
        return um._integration_recorded(ctx)

    def v_serves(ctx: OpContext) -> VerifyResult:
        _seed(ctx)
        return um._serves_as_umami(ctx.url) if ctx.url else VerifyResult(False, "no served url")

    def v_capacity(ctx: OpContext) -> VerifyResult:
        _seed(ctx)
        return _capacity_loaded(ctx, capacity_target)

    ops = [
        TierOperation(
            op_id="deploy-serve", op_type=PROVISION,
            task=("Deploy the umami web analytics application together with its database so it is fully "
                  "running and serving over HTTP and can authenticate (the database must be connected)."),
            verify=v_deploy, durable=True),
        TierOperation(
            op_id="mutate:register", op_type=OPERATE_MUTATE,
            task=(f"In the running umami, create a new ADMIN user with username '{probe['username']}' and "
                  f"password '{probe['password']}', by whatever method your deployment allows. It must be "
                  "able to log in afterward."),
            verify=v_register, depends_on=("deploy-serve",), durable=True),
        TierOperation(
            op_id="integrate", op_type=OPERATE_MUTATE,
            task=(f"Log in to umami as the '{probe['username']}' account and, UNDER that account, create a "
                  f"umami website named '{probe['website_name']}' (umami websites are per-owner). Deploy a "
                  "second simple web page and wire umami's tracking into it for that website, then generate "
                  f"at least one real pageview for the path '{probe['sentinel_path']}'. Its traffic must "
                  "show up inside umami, owned by that account."),
            verify=v_integrate, depends_on=("deploy-serve", "mutate:register"), durable=True),
        TierOperation(
            op_id="harden", op_type=OPERATE_MUTATE,
            task=("Harden this umami deployment toward production-grade using whatever its cloud offers: "
                  "make the Postgres highly-available or replicated and enable automated backups, add a "
                  "health/restart policy, and run more than one app instance if the surface allows it. "
                  "umami must keep serving throughout."),
            verify=v_serves, depends_on=("deploy-serve",),
            durable=False),   # its payoff is proven by surviving the fault; here just confirm it still serves
        TierOperation(
            op_id="capacity", op_type=OPERATE_MUTATE,
            task=(f"Generate a LARGE volume of real analytics traffic through umami's OWN tracking ingest "
                  f"for the website '{probe['website_name']}': record at least {capacity_target} pageview "
                  "events (write a small loop or load generator against the tracking endpoint the app "
                  "exposes; batching and concurrency are fine). They must all be recorded in umami under "
                  "that website."),
            verify=v_capacity, depends_on=("deploy-serve", "integrate"), durable=True),
        TierOperation(
            op_id="fault", op_type=OPERATE_MUTATE,
            task=("Inject a fault to test resilience: restart or kill the datastore (force a failover if it "
                  "is highly-available), and separately restart the app the way your deployment supports. "
                  "After the fault, umami must be serving again."),
            verify=v_serves, depends_on=("deploy-serve", "harden"),
            durable=False),   # durability of the DATA under this fault is the terminal conjunction below
    ]
    return TierInstance(
        name="umami-hard", tier="hard", operations=ops,
        teardown_hint=("the umami application, its managed database (and any replicas or backups you added "
                       "when hardening), the second website deployed for the integration, and every VM / "
                       "instance / volume / floating IP / proxy host created for them"))
