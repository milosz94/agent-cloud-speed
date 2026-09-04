"""Hard tier, instance #1: umami under a capacity layer + a FIXED fault battery.

This instance implements PAPER Part 5's Hard paragraph literally. Each requirement there maps to one
thing here, because a harness that implements a weaker protocol than the pre-registration is falsifiable
by any reviewer who reads the published tool:

  spec                                            here
  ----------------------------------------------  -------------------------------------------------
  hardening: agent told the CRITERION, not method  ``harden`` states the criterion (survive faults with
                                                   no data loss, recover on its own) and lists methods
                                                   only as "whatever the cloud offers" examples
  capacity: >= 10^5 events, FROM AN OFF-CLOUD      ``capacity`` is a HARNESS action (no agent turn):
  DRIVER, exact expected count, known early/       ``_bulk_load`` posts events from this host to the
  middle/late sentinel rows, BEFORE the faults     app's own ingest path, with three sentinel paths at
                                                   the start / middle / end of the stream, and the
                                                   faults depend on it so it always runs first
  a FIXED fault battery: kill the datastore,       four fixed operations, one per fault, in that order
  kill the app, reboot the host, partition the
  datastore link
  after EACH fault: re-run a tracked visit, and    ``_after_fault`` re-runs a tracked visit, re-reads
  re-verify the FULL dataset (exact row count      the EXACT count and EVERY sentinel, and times the
  plus every sentinel), with recovery time         recovery (poll until serving again)
  measured
  behavior-under-load reads: ingest throughput,    ``_bulk_load`` records events/s and p95 write latency;
  p95 write latency at load, recovery time as a    each fault records recovery_s against the dataset
  function of dataset size                         size, so the pair is reportable
  serverless fallback: a boundary fault THE        the host-reboot fault carries ``harness_fallback``
  HARNESS controls                                 guidance and the battery still scores it

The agent owns the METHOD for every mutation; the runner owns VERIFICATION and, for the capacity layer,
the LOAD itself (the spec's "off-cloud driver": ground truth may not be agent-reported). Success is the
terminal conjunction: after the whole battery, umami serves, the probe admin authenticates, the
integration pageview is still recorded, and the full bulk-loaded dataset is still intact.

``capacity_target`` defaults to the spec's 10^5. Lower it explicitly for a smoke run; a study run must
not, and the value is recorded in the result so a table can never silently mix scales.
"""
from __future__ import annotations

import time
from typing import Optional

from . import umami_medium as um
from ._http import http
from ..operation import OPERATE_MUTATE, PROVISION
from ..suite import OpContext, TierInstance, TierOperation, VerifyResult

# The spec's capacity target. 10^5 tracked events, loaded off-cloud, before any fault.
SPEC_CAPACITY_TARGET = 100_000

# How the three ground-truth sentinel rows are named. They sit at the START, MIDDLE and END of the event
# stream so a PARTIAL loss (the datastore lost its tail, or an un-flushed buffer) is caught, not just a
# total one, which is exactly why the spec asks for early/middle/late rather than one marker.
_SENTINEL_POSITIONS = ("early", "middle", "late")


def _sentinel_path(token: str, which: str) -> str:
    return f"/acs-cap-{which}-{token}"


def _website_id(ctx: OpContext, token: str) -> Optional[str]:
    """The umami website id the capacity load targets: the probe's own website, looked up through umami's
    API as the probe admin (surface-independent, never the agent's report)."""
    probe = ctx.state["probe"]
    for s in um._list_websites(ctx.url or "", token):
        blob = f"{s.get('name', '')} {s.get('domain', '')}".lower()
        if probe["website_name"].lower() in blob:
            wid = s.get("id") or s.get("websiteId")
            if wid:
                return str(wid)
    return None


def _pageview_count(url: str, token: str, website_id: str, path: Optional[str] = None) -> Optional[float]:
    """Recorded pageviews for the website over a wide window, optionally for ONE path (the sentinel read).
    None if the read does not parse, so a shape drift is 'unconfirmed' and the check fails closed rather
    than declaring a lost dataset durable."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 90 * 24 * 3600 * 1000
    end_ms = now_ms + 24 * 3600 * 1000
    base = f"{url.rstrip('/')}/api/websites/{website_id}"
    code, _, parsed = http("GET", f"{base}/metrics?type=path&startAt={start_ms}&endAt={end_ms}",
                           bearer=token, timeout_s=45)
    if code == 200 and isinstance(parsed, list):
        total = 0.0
        for row in parsed:
            if not isinstance(row, dict):
                continue
            if path is not None and not um._path_matches(str(row.get("x", "")), path):
                continue
            try:
                total += float(row.get("y", 0) or 0)
            except (TypeError, ValueError):
                pass
        return total
    if path is not None:
        return None
    code, _, parsed = http("GET", f"{base}/stats?startAt={start_ms}&endAt={end_ms}",
                           bearer=token, timeout_s=45)
    if code == 200 and isinstance(parsed, dict) and isinstance(parsed.get("pageviews"), (dict, int, float)):
        pv = parsed["pageviews"]
        val = pv.get("value") if isinstance(pv, dict) else pv
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    return None


def _send_event(url: str, website_id: str, host: str, path: str) -> tuple:
    """One tracked pageview through the app's OWN ingest path. Returns (ok, latency_seconds)."""
    t0 = time.monotonic()
    code, _, _ = http("POST", f"{url.rstrip('/')}/api/send",
                      body={"type": "event",
                            "payload": {"website": website_id, "hostname": host, "url": path,
                                        "title": "acspeed capacity", "referrer": ""}},
                      headers={"Content-Type": "application/json"}, timeout_s=30)
    return (code is not None and 200 <= code < 300), time.monotonic() - t0


def _bulk_load(ctx: OpContext, target: int) -> dict:
    """The capacity layer's OFF-CLOUD DRIVER (spec: the load does not come from the agent).

    Posts ``target`` tracked events from THIS host through the app's own ingest path, placing the three
    ground-truth sentinels at the start, middle and end of the stream, and records the behavior-under-load
    reads the spec asks for: ingest throughput (events/s) and p95 write latency. The exact expected count
    is ``baseline + accepted``, captured here rather than assumed, so the post-fault re-verify can compare
    against a real number instead of a threshold."""
    url = ctx.url or ""
    token = um._probe_token(ctx)
    if not token or not url:
        return {"ok": False, "error": "no probe token or url for the capacity load"}
    wid = _website_id(ctx, token)
    if not wid:
        return {"ok": False, "error": "the capacity website was not found in umami"}
    host = um._host(url)
    run_token = str(ctx.state.get("probe", {}).get("run_token") or ctx.state.get("run_token") or "cap")
    sentinels = {w: _sentinel_path(run_token, w) for w in _SENTINEL_POSITIONS}

    baseline = _pageview_count(url, token, wid)
    if baseline is None:
        return {"ok": False, "error": "could not read the pre-load baseline count"}

    positions = {0: sentinels["early"], max(0, target // 2): sentinels["middle"],
                 max(0, target - 1): sentinels["late"]}
    latencies: list = []
    accepted = 0
    failed = 0
    t0 = time.monotonic()
    for i in range(target):
        path = positions.get(i, f"/acs-cap/{i % 50}")
        ok, dt = _send_event(url, wid, host, path)
        latencies.append(dt)
        if ok:
            accepted += 1
        else:
            failed += 1
            # A sustained ingest outage means the load is not real; stop rather than spend an hour
            # posting into a dead endpoint and then reporting a scale that never landed.
            if failed > 50 and accepted == 0:
                break
    elapsed = max(1e-6, time.monotonic() - t0)
    latencies.sort()
    p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))] if latencies else None

    reads = {
        "ok": accepted > 0,
        "target": target,
        "accepted": accepted,
        "failed": failed,
        "baseline_count": baseline,
        "expected_count": baseline + accepted,
        "sentinels": sentinels,
        "elapsed_s": round(elapsed, 2),
        # behavior-under-load reads (spec)
        "ingest_throughput_eps": round(accepted / elapsed, 2),
        "p95_write_latency_s": round(p95, 4) if p95 is not None else None,
        "website_id": wid,
    }
    ctx.state["capacity"] = reads
    return reads


def _read_dataset(ctx: OpContext) -> dict:
    """Re-read the FULL dataset: the exact recorded count plus every sentinel path, independently, as the
    probe admin. This is the integrity-at-scale read the spec requires after every fault."""
    cap = ctx.state.get("capacity") or {}
    url = ctx.url or ""
    token = um._probe_token(ctx)
    if not token or not url or not cap.get("website_id"):
        return {"ok": False, "error": "cannot read the dataset (no token, url, or website id)"}
    wid = cap["website_id"]
    count = _pageview_count(url, token, wid)
    sent = {}
    for which, path in (cap.get("sentinels") or {}).items():
        n = _pageview_count(url, token, wid, path=path)
        sent[which] = (n or 0) >= 1
    return {"ok": count is not None, "count": count, "expected": cap.get("expected_count"),
            "sentinels_present": sent, "all_sentinels": all(sent.values()) if sent else False}


def _capacity_verified(ctx: OpContext) -> VerifyResult:
    """The load landed AT SCALE with its exact count and all three sentinels."""
    cap = ctx.state.get("capacity") or {}
    if not cap.get("ok"):
        return VerifyResult(False, f"capacity load did not run: {cap.get('error', 'unknown')}")
    if cap.get("accepted", 0) < cap.get("target", 0):
        return VerifyResult(False,
                            f"ingest accepted only {cap.get('accepted')} of {cap.get('target')} events",
                            measured=cap)
    ds = _read_dataset(ctx)
    if not ds.get("ok"):
        return VerifyResult(False, "could not read the dataset back (VALIDATE-LIVE)", measured=ds)
    exact = ds.get("count") is not None and ds["count"] >= (ds.get("expected") or 0)
    return VerifyResult(bool(exact and ds.get("all_sentinels")),
                        f"recorded {int(ds.get('count') or 0)} of expected {int(ds.get('expected') or 0)}; "
                        f"sentinels {ds.get('sentinels_present')}; "
                        f"{cap.get('ingest_throughput_eps')} events/s, p95 write "
                        f"{cap.get('p95_write_latency_s')}s",
                        measured={**cap, "readback": ds})


def _await_serving(ctx: OpContext, budget_s: float = 600.0, poll_s: float = 5.0) -> Optional[float]:
    """Seconds until the app serves again, or None if it never did within the budget. This is the spec's
    per-fault RECOVERY TIME, measured by the harness rather than reported by the agent. With no served URL
    there is nothing to poll, so it returns immediately rather than burning the budget."""
    if not ctx.url:
        return None
    t0 = time.monotonic()
    while True:
        if um._serves_as_umami(ctx.url).ok:
            return round(time.monotonic() - t0, 1)
        if time.monotonic() - t0 >= budget_s:
            return None
        time.sleep(poll_s)


def _after_fault(fault_id: str):
    """The spec's per-fault check: recover, re-run a tracked visit, and re-verify the FULL dataset (exact
    count plus every sentinel), recording the recovery time against the dataset size."""
    def _verify(ctx: OpContext) -> VerifyResult:
        ctx.state.setdefault("faults", {})
        recovery_s = _await_serving(ctx)
        cap = ctx.state.get("capacity") or {}
        record = {"recovered": recovery_s is not None, "recovery_s": recovery_s,
                  "dataset_size": cap.get("expected_count")}
        if recovery_s is None:
            ctx.state["faults"][fault_id] = {**record, "data_intact": False}
            return VerifyResult(False, f"{fault_id}: never served again within the recovery budget",
                                measured=record)
        # a tracked visit must still be ingestible after the fault (the app path works end to end)
        visit = um._integration_recorded(ctx)
        ds = _read_dataset(ctx)
        intact = bool(ds.get("ok") and ds.get("all_sentinels")
                      and (ds.get("count") or 0) >= (ds.get("expected") or 0))
        record.update({"data_intact": intact, "visit_ok": visit.ok, "count": ds.get("count"),
                       "expected": ds.get("expected"), "sentinels": ds.get("sentinels_present")})
        ctx.state["faults"][fault_id] = record
        return VerifyResult(bool(intact and visit.ok),
                            f"{fault_id}: recovered in {recovery_s}s; dataset "
                            f"{int(ds.get('count') or 0)}/{int(ds.get('expected') or 0)}, sentinels "
                            f"{ds.get('sentinels_present')}; tracked visit {'ok' if visit.ok else 'FAILED'}",
                            measured=record)
    return _verify


# The FIXED battery. Order is the spec's; each is a separate scored operation so "the weighted fraction of
# faults survived with no data loss" is computable per fault instead of collapsing into one pass/fail.
_FAULT_BATTERY = (
    ("fault:kill-datastore",
     "Kill the datastore process or instance backing umami (force a failover if you made it "
     "highly-available). Do not restore data by hand: the deployment must recover on its own."),
    ("fault:kill-app",
     "Kill the umami application process or instance (not the datastore). The deployment must bring it "
     "back on its own, without you redeploying from scratch."),
    ("fault:reboot-host",
     "Reboot the host or instance umami runs on. If your surface is fully serverless and exposes no host, "
     "instead stop the provisioned application resource so the equivalent boundary fault occurs, and say "
     "which you did. The deployment must come back on its own."),
    ("fault:partition-datastore-link",
     "Sever the network path between umami and its datastore (a security-group / firewall rule change is "
     "enough), leave it severed briefly so the app genuinely loses its datastore, then restore the path. "
     "The deployment must recover with no data loss."),
)


def build(probe: Optional[dict] = None, capacity_target: int = SPEC_CAPACITY_TARGET) -> TierInstance:
    """Build the umami Hard instance. ``probe`` is the per-run sentinel (a fresh one is generated if not
    given); ``capacity_target`` is the off-cloud bulk-load size, defaulting to the spec's 10^5. Lower it
    only for a smoke run: the value is carried in the result so no table can silently mix scales."""
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

    def a_capacity(ctx: OpContext) -> dict:
        _seed(ctx)
        return {"capacity": _bulk_load(ctx, capacity_target)}

    def v_capacity(ctx: OpContext) -> VerifyResult:
        _seed(ctx)
        return _capacity_verified(ctx)

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
            # CRITERION, not method (spec): the judgement about how to be resilient is part of the test.
            task=("Make this deployment resilient: it must survive infrastructure faults with NO DATA LOSS "
                  "and recover on its own, without you intervening. Faults will be injected shortly, "
                  "including losing the datastore, losing the app, losing the host, and losing the network "
                  "path between app and datastore. How you achieve that is your decision, using whatever "
                  "the cloud offers. umami must keep serving throughout this change."),
            verify=v_serves, depends_on=("deploy-serve",),
            durable=False),   # its payoff is proven by surviving the battery below
        TierOperation(
            op_id="capacity", op_type=OPERATE_MUTATE,
            # HARNESS-DRIVEN (spec: "from an off-cloud driver"), so the ground truth is never agent-reported.
            task=("(harness-driven capacity load; no agent action required)"),
            harness_action=a_capacity,
            verify=v_capacity, depends_on=("deploy-serve", "integrate"), durable=True),
    ] + [
        TierOperation(
            op_id=fid, op_type=OPERATE_MUTATE, task=task,
            verify=_after_fault(fid),
            # every fault comes AFTER the capacity layer, so durability is always tested at scale
            depends_on=("deploy-serve", "harden", "capacity"), durable=False)
        for fid, task in _FAULT_BATTERY
    ]
    return TierInstance(
        name="umami-hard", tier="hard", operations=ops,
        teardown_hint=("the umami application, its managed database (and any replicas or backups you added "
                       "when hardening), the second website deployed for the integration, and every VM / "
                       "instance / volume / floating IP / proxy host created for them"))
