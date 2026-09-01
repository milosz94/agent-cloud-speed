"""Medium tier, instance #1: umami (analytics app + its datastore) plus a STANDARD second site wired
into it, with end-to-end integration proof and restart-durability as a goal.

This is ONE instance of the Medium operation graph. The engine (``acspeed/suite.py``) knows nothing
about umami; everything umami-specific lives in the verify predicates below, which the RUNNER owns.
The agent owns the METHOD for every mutation.

The operation graph (Medium), and the seven independent boundary reads it verifies:

  1. deploy-serve    (PROVISION)      umami serves and identifies as umami.               [R1]
  2. mutate:register (OPERATE_MUTATE) a known probe ADMIN user exists (durable identity). [R2]
  3. deploy-site-b   (PROVISION)      a STANDARD second site is stood up and serves.      [R3]
  4. integrate       (OPERATE_MUTATE) site B carries umami's tracking wiring,             [R4]
                                       and a HARNESS headless-browser visit to site B is
                                       recorded as a pageview in umami (end to end).       [R5]
  then the restart-durability GOAL: restart, and if the user or the pageview did not
  survive, re-architect and try again until they do (design-owner ruling):
                                       after a restart, umami serves again,               [R6]
                                       and the user + pageview still survive.             [R7]

Why "the visit is recorded" is a REAL read, not a self-report: the agent is explicitly told NOT to
generate any traffic. The ONLY visit to site B is the one the runner drives with a real headless
browser (``_visit.headless_visit``); site B's served page carries the umami snippet, so that visit
fires umami's beacon and a pageview appears. The read requires the umami pageview count for site B to
INCREASE from its pre-visit baseline. A pageview can therefore exist only if: site B actually serves
(R3), its HTML actually carries the correct wiring (R4), umami actually ingests (R1 + a live datastore),
and the account owns the website (R2). One number closes the whole loop, and it cannot be faked by a
curl to the collect API because the harness, not the agent, is the sole visitor.

Site B is STANDARDIZED: the same small Node.js application for every run and every cloud (its source lives
in ``site_b/`` and is embedded verbatim into the deploy-site-b task), so the second app is a constant under
test rather than an agent-varying artifact. It serves the same page on every path (a catch-all route), so
nothing per-run is baked in. The shipped source is deliberately NEUTRAL: no analytics, no umami reference,
and nothing naming this a benchmark, so the deploy-site-b step never telegraphs the integration to come.
The agent adds umami's tracking snippet only during the ``integrate`` operation, learning that requirement
only when it reaches it (this is the online / requirements-revealed-incrementally instance, Medium A).

VALIDATE-LIVE: the exact umami endpoint shapes (login, /api/websites, the stats/metrics reads) are
umami v2's and can shift between minor versions; these predicates are written against umami v2 and MUST
be validated against a live umami before the instance is trusted (a verify that cannot fail is not a
control). The stats reads degrade to "unconfirmed" on an unrecognized shape, never a false pass.
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional
from urllib.parse import urlsplit

from ..suite import DurabilityGoal, OpContext, TierInstance, TierOperation, VerifyResult
from ..operation import OPERATE_MUTATE, PROVISION
from ._http import http
from ._visit import headless_visit


# The STANDARD second site: a small Node.js application whose source lives in ``site_b/`` and is embedded
# verbatim into the deploy-site-b task. Identical for every run/cloud (a constant under test). It serves the
# same page on every path (a catch-all route), so nothing per-run is baked in. The shipped source is
# deliberately NEUTRAL (no analytics, no umami reference, nothing naming this a benchmark), so deploy-site-b
# never leaks the integration that follows; the agent wires umami in only at the integrate step.
_SITE_B_DIR = os.path.join(os.path.dirname(__file__), "site_b")
# Only source is shipped: no node_modules, no lockfile, no README (the agent installs deps + picks how to deploy).
_SITE_B_FILES = ("package.json", "server.js", os.path.join("public", "index.html"))
# The canonical-page signature R3 reads: the page title, neutral and distinctive (no benchmark identity leaked).
SITE_B_MARKER = "alcove, save what matters"


def _load_site_b_sources() -> list:
    """The shipped source of the standard second site, read from ``site_b/`` (single source of truth).
    Returns ``[(relpath, content), ...]`` for embedding into the deploy-site-b task."""
    out = []
    for rel in _SITE_B_FILES:
        with open(os.path.join(_SITE_B_DIR, rel), "r", encoding="utf-8") as fh:
            out.append((rel.replace(os.sep, "/"), fh.read()))
    return out


def _canonical_site_b_html() -> str:
    """Site B's page as first deployed (what R3/R4 read): the standard page, no umami wiring."""
    for rel, content in _load_site_b_sources():
        if rel.endswith("index.html"):
            return content
    return ""


def _site_b_manifest() -> str:
    """The shipped source as a file manifest, ready to drop into the deploy-site-b task text."""
    parts = []
    for rel, content in _load_site_b_sources():
        parts.append("===== FILE: {} =====\n{}\n".format(rel, content.rstrip()))
    return "\n".join(parts)

# umami's tracker script filename (stable across v2); the wiring read requires the snippet's src to
# point at THIS run's umami origin and to carry a non-empty website id.
UMAMI_TRACKER_FILE = "script.js"


def _make_probe() -> dict:
    """A per-run sentinel: a unique admin username + password, a website name and a unique path. Unique
    so re-runs never collide and the runner knows the exact thing to look for."""
    suffix = os.urandom(4).hex()
    return {
        "username": f"acspeed_probe_{suffix}",
        "password": f"Acspeed-{suffix}-pw",
        "website_name": f"acspeed-site-{suffix}",
        "sentinel_path": f"/acspeed-sentinel-{suffix}",
        "suffix": suffix,
    }


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


# ---- umami read surface (runner-owned; never consults the agent) -------------------------------

def _serves_as_umami(url: str) -> VerifyResult:
    code, text, _ = http("GET", url, timeout_s=20)
    if code is None:
        return VerifyResult(False, "root did not answer")
    is_umami = "umami" in text.lower()
    ok = code < 500 and is_umami
    return VerifyResult(ok, f"http {code}, umami-signature={'yes' if is_umami else 'no'}",
                        measured={"http_code": code, "umami_signature": is_umami})


def _login(url: str, username: str, password: str) -> Optional[str]:
    """POST /api/auth/login; return the bearer token, or None. Stable across umami v2."""
    code, _, parsed = http("POST", url.rstrip("/") + "/api/auth/login",
                           body={"username": username, "password": password}, timeout_s=20)
    if code == 200 and isinstance(parsed, dict):
        tok = parsed.get("token")
        if isinstance(tok, str) and tok:
            return tok
    return None


def _umami_url(ctx: OpContext) -> Optional[str]:
    """The umami origin, pinned at deploy time so a later site-B deploy (which moves ``ctx.url``) or a
    re-architecture never makes the umami reads hit the wrong host. Falls back to ``ctx.url`` if it
    still looks like umami (covers a restart that legitimately changed umami's own URL)."""
    u = ctx.state.get("umami_url")
    if u:
        code, text, _ = http("GET", u, timeout_s=15)
        if code is not None and "umami" in (text or "").lower():
            return u
    if ctx.url:
        code, text, _ = http("GET", ctx.url, timeout_s=15)
        if code is not None and "umami" in (text or "").lower():
            ctx.state["umami_url"] = ctx.url
            return ctx.url
    return u  # last known; the caller's read will fail loudly if it is gone


def _probe_token(ctx: OpContext) -> Optional[str]:
    probe = ctx.state["probe"]
    tok = ctx.state.get("probe_token")
    uu = _umami_url(ctx)
    if tok and uu:
        return tok
    if uu:
        tok = _login(uu, probe["username"], probe["password"])
        if tok:
            ctx.state["probe_token"] = tok
        return tok
    return None


def _list_websites(url: str, token: str) -> list:
    code, _, parsed = http("GET", url.rstrip("/") + "/api/websites", bearer=token, timeout_s=20)
    if code != 200:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
        return parsed["data"]
    return []


def _find_site_b_website(ctx: OpContext, token: str) -> Optional[dict]:
    """The umami website the agent created for site B, owned by the probe user. Prefer a domain that
    matches site B's host; fall back to the sentinel website name."""
    uu = _umami_url(ctx)
    if not uu:
        return None
    sb_host = _host(ctx.state.get("site_b_url", ""))
    name = ctx.state["probe"]["website_name"].lower()
    sites = _list_websites(uu, token)
    by_domain = [s for s in sites if sb_host and sb_host in f"{s.get('domain','')}".lower()]
    by_name = [s for s in sites if name in f"{s.get('name','')} {s.get('domain','')}".lower()]
    for pool in (by_domain, by_name):
        if pool:
            return pool[0]
    return None


def _path_matches(row_x: str, path: str) -> bool:
    """True if a umami per-URL row refers to our sentinel path. The sentinel path carries a unique random
    suffix, so a containment test is safe (no false match) and tolerates a trailing slash or query string."""
    a = (row_x or "").split("?")[0].rstrip("/").lower()
    b = (path or "").split("?")[0].rstrip("/").lower()
    return bool(b) and (a == b or b in a)


def _pageview_count(ctx: OpContext, token: str, website_id: str, path: Optional[str] = None) -> Optional[int]:
    """Pageview count for ``website_id``. With ``path`` set, count ONLY that exact URL path (the unique
    per-run sentinel path) via umami's per-URL breakdown, so the number is attributable to the harness's
    single visit and CANNOT be moved by traffic to any other page - the agent's own test visits, bots, or
    the site root (the agent may create throwaway pages/websites while it works; a website-wide count would
    fold those in). Without ``path``, the whole-website aggregate (legacy / Hard tier). None ONLY if the
    read shape is unrecognized (best-effort: a drift is 'unconfirmed', never a fabricated number); a parsed
    breakdown that simply has no row for our path is a real 0, not a drift."""
    uu = _umami_url(ctx)
    if not uu:
        return None
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 90 * 24 * 3600 * 1000
    end_ms = now_ms + 24 * 3600 * 1000
    base = f"{uu.rstrip('/')}/api/websites/{website_id}"
    if path:
        # umami v2 per-URL breakdown [{"x": "/path", "y": count}]. VALIDATED LIVE 2026-08-30: this umami
        # returns the list under type=PATH (type=url returned null), so try path first; url is a fallback
        # for any build that aliases it the other way. Whichever returns a list wins (path is authoritative).
        for mtype in ("path", "url"):
            code, _, parsed = http("GET", f"{base}/metrics?type={mtype}&startAt={start_ms}&endAt={end_ms}",
                                   bearer=token, timeout_s=20)
            if code == 200 and isinstance(parsed, list):
                total = 0
                for row in parsed:
                    if isinstance(row, dict) and _path_matches(str(row.get("x", "")), path):
                        try:
                            total += int(float(row.get("y", 0)))
                        except (TypeError, ValueError):
                            pass
                return total
        return None
    # aggregate (legacy): /stats pageviews.value, then /metrics?type=path summed over all paths
    code, _, parsed = http("GET", f"{base}/stats?startAt={start_ms}&endAt={end_ms}", bearer=token, timeout_s=20)
    if code == 200 and isinstance(parsed, dict) and isinstance(parsed.get("pageviews"), (dict, int, float)):
        pv = parsed["pageviews"]
        val = pv.get("value") if isinstance(pv, dict) else pv
        try:
            return int(float(val))
        except (TypeError, ValueError):
            pass
    code, _, parsed = http("GET", f"{base}/metrics?type=path&startAt={start_ms}&endAt={end_ms}", bearer=token, timeout_s=20)
    if code == 200 and isinstance(parsed, list):
        total = 0
        for row in parsed:
            if isinstance(row, dict):
                try:
                    total += int(float(row.get("y", 0)))
                except (TypeError, ValueError):
                    pass
        return total
    return None


# ---- the seven boundary reads ------------------------------------------------------------------

def _r1_umami_serves(ctx: OpContext) -> VerifyResult:
    uu = _umami_url(ctx)
    return _serves_as_umami(uu) if uu else VerifyResult(False, "no umami url")


def _r2_user_exists(ctx: OpContext) -> VerifyResult:
    probe = ctx.state["probe"]
    uu = _umami_url(ctx)
    if not uu:
        return VerifyResult(False, "no umami url")
    tok = _login(uu, probe["username"], probe["password"])
    if tok:
        ctx.state["probe_token"] = tok
        return VerifyResult(True, f"probe user {probe['username']} authenticates")
    return VerifyResult(False, f"probe user {probe['username']} could not authenticate")


def _r3_site_b_serves(ctx: OpContext) -> VerifyResult:
    """Site B serves the STANDARD canonical page (title/heading present) at its own URL. Also records
    site B's URL from ``ctx.url`` (set by the deploy-site-b provision turn)."""
    if ctx.url and ctx.url != ctx.state.get("umami_url"):
        ctx.state["site_b_url"] = ctx.url  # the just-provisioned site B
    sb = ctx.state.get("site_b_url")
    if not sb:
        return VerifyResult(False, "second site's URL was never resolved (no distinct provision URL)")
    code, text, _ = http("GET", sb, timeout_s=20)
    if code is None:
        return VerifyResult(False, f"site B ({sb}) did not answer")
    canonical = SITE_B_MARKER in text.lower()
    ok = code < 500 and canonical
    return VerifyResult(ok, f"site B http {code}, canonical-page={'yes' if canonical else 'no'}",
                        measured={"site_b_url": sb, "http_code": code})


_SCRIPT_RE = re.compile(r"<script\b[^>]*>", re.IGNORECASE)


def _service_key(host: str, token: Optional[str]) -> str:
    """The stable service identity shared by a provider's ALTERNATE hostnames for ONE service: the
    '<name>-<token>' leading label. Cloud Run serves a single service at TWO public URLs
    (umami-<token>-<hash>-<region>.a.run.app and umami-<token>-<projnum>.<region>.run.app); both carry
    '<name>-<token>' but neither host is a substring of the other, so a full-host match wrongly rejects
    a snippet that points at the same umami via its other URL. Matching '<name>-<token>' accepts either
    URL yet still rejects the second site (different name) and a foreign run's umami (different token).
    Returns '' when the token is unknown, so the caller falls back to the full-host match."""
    if not host or not token:
        return ""
    i = host.find(token)
    if i < 0:
        return ""
    return host[: i + len(token)]  # '<name>-<token>', invariant across a provider's alternate URLs


def _wiring_ok(html: str, umami_host: str, umami_key: str = "") -> tuple:
    """Does site B's served HTML carry the umami tracker snippet, pointing at THIS run's umami and
    carrying a non-empty website id? Returns (ok, website_id_or_empty). The umami match is by service
    IDENTITY (``umami_key`` = '<name>-<token>') when known, so either of Cloud Run's two URLs for the
    one umami service is accepted; it falls back to the full host only when the key is unknown."""
    for tag in _SCRIPT_RE.findall(html):
        low = tag.lower()
        if UMAMI_TRACKER_FILE not in low:
            continue
        src = re.search(r'src\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
        wid = re.search(r'data-website-id\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if not src or not wid:
            continue
        src_host = _host(src.group(1))
        if umami_key:
            if umami_key not in src_host:
                continue  # not THIS run's umami service (by name+token), under EITHER of its URLs
        elif umami_host and umami_host not in src_host:
            continue  # no key known: exact-host fallback (points at some other umami, not this run's)
        if wid.group(1).strip():
            return True, wid.group(1).strip()
    return False, ""


def _r4_wiring_present(ctx: OpContext) -> VerifyResult:
    sb = ctx.state.get("site_b_url")
    if not sb:
        return VerifyResult(False, "no site B url to read wiring from")
    uu = _umami_url(ctx)
    umami_host = _host(uu) if uu else ""
    umami_key = _service_key(umami_host, ctx.state.get("run_token"))  # match by service identity, not exact host
    code, text, _ = http("GET", sb, timeout_s=20)
    if code is None:
        return VerifyResult(False, "site B did not answer for the wiring read")
    ok, wid = _wiring_ok(text, umami_host, umami_key)
    if ok:
        ctx.state["site_b_website_id"] = wid
    return VerifyResult(ok, f"site B HTML {'carries' if ok else 'missing'} umami wiring"
                            + (f" (website id {wid})" if ok else ""),
                        measured={"website_id": wid})


# Propagation windows for a host that is not immediately consistent after the agent's redeploy: a Cloud Run
# revision migration (seconds) or a CDN edge cache. Measured on aws 2026-08-31: with the site fronted by
# CloudFront, the wired page + the recorded pageview landed within a few minutes, AFTER the old 60s + single
# 6s windows had already failed the op (durability cycle 2, minutes later, then saw them fine). So the fix is
# to give the integration time to become consistent WITHIN the op, not to score it on durability. Module
# constants so tests drop them to 0. This does NOT lower the bar: if a window expires with the wiring still
# absent or the pageview still unrecorded, the op still fails.
_WIRING_POLL_MAX_S = 180.0
_INGEST_POLL_MAX_S = 90.0
_INGEST_POLL_INTERVAL_S = 5.0


def _wiring_present_polled(ctx: OpContext, max_wait_s: Optional[float] = None,
                          interval_s: float = 5.0) -> VerifyResult:
    """R4, but POLLED for propagation. An immutable / serverless host serves the PREVIOUS revision for a few
    seconds after the agent redeploys (Cloud Run migrates traffic to a new revision with a lag; a CDN edge
    caches), so one read can miss wiring that is genuinely landing. Retry until the wiring is present or the
    window expires. Measured 2026-08-31 (gcp): a run failed integrate 'site B HTML missing umami wiring' at
    the read moment, yet the SAME wiring was present by the durability re-verify - it simply had not
    propagated yet. This does NOT lower the bar: a window that expires with the wiring still absent returns
    the last (failing) read, so a genuinely un-wired page still fails."""
    if max_wait_s is None:
        max_wait_s = _WIRING_POLL_MAX_S
    deadline = time.monotonic() + max_wait_s
    r = _r4_wiring_present(ctx)
    while not r.ok and time.monotonic() < deadline:
        time.sleep(interval_s)
        r = _r4_wiring_present(ctx)
    return r


def _r5_visit_recorded(ctx: OpContext) -> VerifyResult:
    """The end-to-end proof, made unfakeable two ways: (1) the website id is the EXACT one WIRED into the
    served page (captured by R4) - the id the beacon fires with - not a fuzzy re-lookup that could land on a
    different umami website the agent created (probe + throwaways); (2) the count is scoped to the UNIQUE
    per-run sentinel path, so only the harness's single visit to that path can move it. Read the sentinel-
    path baseline, drive a REAL headless-browser visit to that path (the only visitor), require +1."""
    tok = _probe_token(ctx)
    if not tok:
        return VerifyResult(False, "could not authenticate to umami to read pageviews")
    # authoritative website id = the one wired into the served page (R4). Fall back to a fuzzy lookup only
    # if the wiring read has not run yet (e.g. R5 called standalone in a reestablish before R4).
    wid = ctx.state.get("site_b_website_id")
    if not wid:
        site = _find_site_b_website(ctx, tok)
        wid = str(site.get("id") or site.get("websiteId") or "") if site else ""
    if not wid:
        return VerifyResult(False, "no umami website id for site B (not wired into the page, none found)")
    wid = str(wid)
    sb = ctx.state.get("site_b_url")
    if not sb:
        return VerifyResult(False, "no site B url to visit")

    path = ctx.state["probe"]["sentinel_path"]
    before = _pageview_count(ctx, tok, wid, path=path)
    if before is None:
        # a flaky/unrecognized baseline read must be UNCONFIRMED, never coerced to 0 (that would let a
        # stale post-restart count satisfy after>0 with no new visit landing - a false pass).
        return VerifyResult(False, "umami sentinel-path pageview read returned an unrecognized shape (VALIDATE-LIVE)")
    visit_url = sb.rstrip("/") + path
    ok_visit, engine = headless_visit(visit_url)
    if not ok_visit:
        if engine == "none":
            return VerifyResult(False, "no headless browser available on the harness host "
                                       "(install playwright or chromium); cannot verify the visit")
        return VerifyResult(False, f"headless visit to the sentinel path failed ({engine})")
    # umami ingest is asynchronous, and behind a CDN the wired page + beacon can lag; POLL the count until it
    # rises above the baseline or the ingest window expires, rather than a single fixed sleep (a single 6s
    # read failed a real aws CloudFront-fronted deploy whose pageview landed a bit later, cf. _INGEST_POLL_MAX_S).
    deadline = time.monotonic() + _INGEST_POLL_MAX_S
    time.sleep(min(_INGEST_POLL_INTERVAL_S, 6.0))
    after = _pageview_count(ctx, tok, wid, path=path)
    while (after is None or after <= before) and time.monotonic() < deadline:
        time.sleep(_INGEST_POLL_INTERVAL_S)
        after = _pageview_count(ctx, tok, wid, path=path)
    if after is None:
        return VerifyResult(False, "umami sentinel-path pageview read returned an unrecognized shape (VALIDATE-LIVE)")
    ok = after > before
    if ok:
        # record the surviving count + the exact path/id so the restart-durability read (R7) checks the
        # SAME sentinel-path count survived, and the reestablish hook can raise the floor.
        ctx.state["pageview_floor"] = after
        ctx.state["pageview_path"] = path
        ctx.state["site_b_website_id"] = wid
    return VerifyResult(ok,
                        f"sentinel-path pageviews {before} -> {after} after a real headless visit ({engine})"
                        + ("" if ok else "; the visit did not register"),
                        measured={"before": before, "after": after, "website_id": wid, "path": path, "engine": engine})


def _r7_pageview_persists(ctx: OpContext) -> VerifyResult:
    """R7 (state persistence across a restart): the earlier recorded pageview is STILL counted - the
    current count has not dropped below the floor established at integrate time. No new visit: this
    reads whether the historical events survived, which is what 'the pageview still present' means."""
    floor = ctx.state.get("pageview_floor")
    wid = ctx.state.get("site_b_website_id")
    path = ctx.state.get("pageview_path")
    tok = _probe_token(ctx)
    if not tok:
        return VerifyResult(False, "could not authenticate to umami to read pageviews")
    if not wid:
        return VerifyResult(False, "no umami website id recorded for site B")
    now = _pageview_count(ctx, tok, str(wid), path=path)
    if now is None:
        return VerifyResult(False, "umami sentinel-path pageview read returned an unrecognized shape (VALIDATE-LIVE)")
    if floor is None:
        return VerifyResult(now >= 1, f"sentinel-path pageview count {now} (no floor recorded)")
    ok = now >= floor
    return VerifyResult(ok, f"sentinel-path pageview count {now} vs floor {floor} "
                            + ("(survived the restart)" if ok else "(events LOST on restart)"),
                        measured={"count": now, "floor": floor, "path": path})


def _reestablish_visit(ctx: OpContext, lost: list) -> None:
    """Durability reestablish hook: if the pageview was lost to a restart, re-drive the harness visit so
    the NEXT restart tests persistence of a freshly-recorded pageview on the (re-architected) storage.
    _r5 refreshes ``pageview_floor`` to the new count on success."""
    if "integrate" in lost:
        _r5_visit_recorded(ctx)


# ---- compatibility for the (not-yet-rebuilt) Hard tier -----------------------------------------
# Hard reuses Medium's register/integrate reads. It has NOT been rebuilt on the new site-B graph, and
# its own capacity layer generates the traffic, so a count-based integration read is the right check
# there. When Hard is rebuilt it should adopt the deploy-site-b + R5 end-to-end read directly.

_probe_login = _r2_user_exists  # same postcondition: the probe user authenticates


def _integration_recorded(ctx: OpContext) -> VerifyResult:
    """LEGACY (Hard tier): the probe user's umami website has >=1 recorded pageview. Weaker than
    Medium's R5 (it does not drive the visit itself), but Hard's capacity layer produces the traffic."""
    tok = _probe_token(ctx)
    if not tok:
        return VerifyResult(False, "could not authenticate to read stats")
    uu = _umami_url(ctx)
    sites = _list_websites(uu, tok) if uu else []
    if not sites:
        return VerifyResult(False, "no websites registered in umami")
    name = ctx.state["probe"]["website_name"].lower()
    ordered = [s for s in sites if name in f"{s.get('name','')} {s.get('domain','')}".lower()] + sites
    unconfirmed = False
    for s in ordered:
        wid = str(s.get("id") or s.get("websiteId") or "")
        if not wid:
            continue
        c = _pageview_count(ctx, tok, wid)
        if c is None:
            unconfirmed = True
        elif c >= 1:
            return VerifyResult(True, f"website {s.get('name', wid)} has {c} pageview(s)",
                                measured={"website_id": wid})
    return VerifyResult(False, "stats returned an unrecognized shape (VALIDATE-LIVE)"
                        if unconfirmed else "no website has a recorded pageview")


# ---- the tier instance -------------------------------------------------------------------------

def build(probe: Optional[dict] = None, durability_max_iters: int = 4,
          plan_upfront: bool = False) -> TierInstance:
    """Build the umami Medium instance. ``probe`` is the per-run sentinel; a fresh one is generated if
    not supplied. ``durability_max_iters`` caps the restart+repair cycles (the durability goal).
    ``plan_upfront`` selects the information regime: False = ONLINE (Medium A, operations revealed one at
    a time); True = DISCLOSED (Medium B, the full plan handed to the agent up front so it can schedule
    with lookahead). Both share the SAME operations, verifies, durability goal and gold; only the regime
    differs, so the paired makespan gap M_online - M_disclosed is the value of plan lookahead."""
    probe = probe or _make_probe()

    def _seed(ctx: OpContext) -> None:
        ctx.state.setdefault("probe", probe)
        if ctx.url and "umami_url" not in ctx.state:
            ctx.state["umami_url"] = ctx.url  # pin umami before a later provision moves ctx.url

    def v_deploy(ctx: OpContext) -> VerifyResult:
        _seed(ctx)
        return _r1_umami_serves(ctx)

    def v_register(ctx: OpContext) -> VerifyResult:
        _seed(ctx)
        return _r2_user_exists(ctx)

    def v_site_b(ctx: OpContext) -> VerifyResult:
        _seed(ctx)
        return _r3_site_b_serves(ctx)

    def v_integrate(ctx: OpContext) -> VerifyResult:
        _seed(ctx)
        r4 = _wiring_present_polled(ctx)  # tolerate an immutable host's redeploy propagation lag
        if not r4.ok:
            return r4  # no point checking traffic if the page is not wired
        if "pageview_floor" not in ctx.state:
            # mid-run: the harness drives the visit end-to-end (R5), count must go 0 -> >=1
            r5 = _r5_visit_recorded(ctx)
            return VerifyResult(r5.ok, f"[wiring] {r4.detail} | [visit] {r5.detail}",
                                measured={"wiring": r4.measured, "visit": r5.measured})
        # terminal / each restart cycle: the earlier pageview must STILL be counted (R7 persistence)
        r7 = _r7_pageview_persists(ctx)
        return VerifyResult(r7.ok, f"[wiring] {r4.detail} | [persist] {r7.detail}",
                            measured={"wiring": r4.measured, "persist": r7.measured})

    def v_umami_live(ctx: OpContext) -> VerifyResult:
        _seed(ctx)
        return _r1_umami_serves(ctx)  # R6: umami serves again after a restart

    site_b_manifest = _site_b_manifest()

    ops = [
        TierOperation(
            op_id="deploy-serve", op_type=PROVISION,
            task=("Deploy the umami web analytics application together with its database so it is fully "
                  "running and serving over HTTP. It must reach its login screen and be able to "
                  "authenticate (its database must be connected, not just the web server up)."),
            verify=v_deploy, durable=True,
        ),
        TierOperation(
            op_id="mutate:register", op_type=OPERATE_MUTATE,
            task=(f"In the running umami, create a new ADMIN user with username '{probe['username']}' "
                  f"and password '{probe['password']}'. The user must be able to log in afterwards."),
            verify=v_register, depends_on=("deploy-serve",), durable=True,
        ),
        TierOperation(
            op_id="deploy-site-b", op_type=PROVISION,
            task=("Stand up ONE second, separate website at its own public URL, distinct from the "
                  "application you deployed earlier. It is "
                  "the small Node.js web application given in full below; deploy exactly this application as "
                  "given, without modifying its pages or adding anything to them. You choose how to run it. "
                  "Deploy exactly one such site - do NOT create duplicates or retry into a second "
                  "deployment.\n\nThe application source:\n\n"
                  f"{site_b_manifest}\n\n"
                  "When it is serving, END your reply with a single line, exactly this format and nothing "
                  "after it:\nSITE_B_URL: <the site's public https URL>"),
            verify=v_site_b, depends_on=("deploy-serve",), durable=True,
        ),
        TierOperation(
            op_id="integrate", op_type=OPERATE_MUTATE,
            task=(f"Log in to umami as '{probe['username']}' and, under THAT account, create a umami "
                  f"website named '{probe['website_name']}' whose domain is the second site's hostname "
                  "(umami websites are owned per-user, so it must belong to that account). Then wire "
                  "umami's tracking snippet into the second site so that the page SERVED at its public "
                  f"URL carries a <script> whose src is your umami's /{UMAMI_TRACKER_FILE} and whose "
                  "data-website-id is that website's id. Before you finish, fetch the second site's "
                  "public URL yourself and confirm that <script> tag is actually present in the served "
                  "HTML.\n"
                  "Do NOT generate any traffic yourself and do NOT insert any pageview manually; leave "
                  "that to the verification."),
            verify=v_integrate, depends_on=("deploy-serve", "mutate:register", "deploy-site-b"),
            durable=True, max_attempts=3,  # wiring an immutable second site can need a redeploy; let it adapt
        ),
    ]

    durability = DurabilityGoal(
        op_id="restart-durability",
        task=("Restart ALL of your running services (umami, its database, and the second site) the way "
              "your deployment supports it. After the restart, the '" + probe["username"] + "' user must "
              "still log in and the recorded pageview must still be counted in umami. If your storage is "
              "ephemeral this state will be lost on restart - that is not acceptable; make it durable."),
        max_iters=durability_max_iters,
        liveness=v_umami_live,
        reestablish=_reestablish_visit,
    )

    return TierInstance(
        name="umami-medium-b" if plan_upfront else "umami-medium", tier="medium",
        operations=ops, durability=durability, plan_upfront=plan_upfront,
        teardown_hint=("the umami application, its managed database, and the second website you deployed "
                       "for the analytics integration"),
        # The two public sites' URL names, so the agent names each '<name>-<token>' and the harness picks the
        # PRIMARY by name (never the second site). The second site's name is the app's own name (its marker).
        primary_url_name="umami",
        second_site_url_name=SITE_B_MARKER.split(",")[0].strip(),  # "alcove"
    )
