#!/usr/bin/env python3
"""Shape the measured runs into the review output, ALL numbers via acspeed (the paper's impl).

Headline (Part 1/2 spine): time-to-serving in SECONDS = critical_platform + critical_agent, each a
distribution with a bootstrap CI (acspeed.repro), never a percentage and never a single number.
First-attempt LIVENESS rate is avg@n with a Wilson CI. Steps (LLM calls) and tokens are the portable
proxies (Part 1). Capability C (raw-infra micro-probes) and the competitive-ratio EFFICIENCY are
separate axes not yet wired (C needs SSH micro-probes; efficiency needs the Part-3 gold): reported as
DEFERRED, never faked. Cold/warm are NOT pooled silently (PAPER change-request C8): runs are shown
individually and the pooled aggregate is labelled as pooled until a real cold/warm fingerprint exists.

Usage:  python3 build_tables.py [--dir /path/to/_measurements/redu/umami]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
from datetime import datetime, timezone

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acspeed import repro  # noqa: E402
from acspeed import gold  # noqa: E402
from acspeed import weighting  # noqa: E402


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (round(100 * p, 1), round(100 * max(0, c - h), 1), round(100 * min(1, c + h), 1))


def _g(d, *path, default=None):
    for k in path:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


def run_cost(rec: dict) -> float | None:
    cs = [r.get("cost") for r in rec.get("rounds", []) if isinstance(r, dict) and r.get("cost")]
    tc = _g(rec, "teardown", "cost")
    if tc:
        cs.append(tc)
    return round(sum(cs), 4) if cs else None


def load_runs(d: str) -> list[dict]:
    out, skipped = [], []
    for p in sorted(glob.glob(os.path.join(d, "run??.json"))):
        try:
            rec = json.load(open(p))
        except Exception as e:  # noqa: BLE001
            skipped.append((os.path.basename(p), f"unreadable: {e}"))
            continue
        if not (isinstance(rec, dict) and "first_attempt_success" in rec
                and isinstance(rec.get("split"), dict)):
            skipped.append((os.path.basename(p), "old/foreign schema (no acspeed split)"))
            continue
        # A run from an unverified platform is provisional and must not be averaged in with runs
        # from the verified one. The table would look normal and the mixture would be invisible.
        if (rec.get("substrate") or {}).get("experimental"):
            skipped.append((os.path.basename(p),
                            "EXPERIMENTAL platform ({}); not pooled".format(
                                (rec.get("substrate") or {}).get("os", "?"))))
            continue
        out.append(rec)
    if skipped:
        print("[skipped] " + ", ".join(f"{n} ({why})" for n, why in skipped))
    return out


def per_run_row(rec: dict) -> dict:
    sp = rec.get("split") or {}
    return {
        "run": rec.get("run"),
        "outcome": rec.get("outcome"),
        "first_ok": rec.get("first_attempt_success"),
        "reached": _g(rec, "recovery", "reached_healthy"),
        "repair_prompts": _g(rec, "recovery", "repair_prompts"),
        "flavor": (fl := _g(rec, "deploy", "flavor")) and fl.replace("flavor_id=", ""),
        "tts_s": rec.get("time_to_serving_s"),
        "agent_end_s": _g(rec, "serving", "agent_finished_at_s"),
        "after_s": _g(rec, "serving", "agent_after_serving_s"),
        "caught_by": _g(rec, "serving", "caught_by"),
        "first_poll": _g(rec, "serving", "served_on_first_poll"),
        "crit_platform_s": sp.get("critical_platform_s"),
        "crit_agent_s": sp.get("critical_agent_s"),
        "overlap_s": sp.get("overlap_s"),
        "boot_s": sp.get("post_handoff_boot_s"),
        "steps": rec.get("steps"),
        "out_tokens": _g(rec, "deploy", "tokens", "output_tokens"),
        "cost_usd": run_cost(rec),
        "warmth": _g(rec, "deploy", "warmth", "clone_or_pull") or _g(rec, "deploy", "warmth", "source"),
        "url_dead": _g(rec, "read_verify", "dead"),
        "shot": _g(rec, "screenshot", "path"),
    }


def est(samples: list, unit: str = "s") -> str:
    xs = [x for x in samples if isinstance(x, (int, float))]
    if not xs:
        return "n/a"
    if len(xs) == 1:
        return f"{xs[0]:.1f}{unit} (n=1, no CI)"
    e = repro.bootstrap_ci(xs, confidence=0.95, iters=2000, seed=0)
    return f"{e.value:.1f}{unit} [95% CI {e.lo:.1f}, {e.hi:.1f}]  (n={e.n})"


def text_render(rows, recs, task) -> str:
    L, W = [], 96
    n = len(rows)
    cloud = recs[0].get("cloud", "?") if recs else "?"
    k_first = sum(1 for r in rows if r["first_ok"] is True)
    k_reach = sum(1 for r in rows if r["reached"] is True)
    fr, flo, fhi = wilson(k_first, n)
    rr, rlo, rhi = wilson(k_reach, n)
    L.append("=" * W)
    L.append(f"AGENT-CLOUD SPEED TEST  |  adapter={cloud}  |  app={task}   (all numbers via acspeed)")
    L.append(f"n={n}   model={recs[0].get('model')}   generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    L.append("=" * W)
    L.append("")
    L.append("HEADLINE  (Part 1/2 spine: wall = critical_platform + critical_agent, in SECONDS)")
    L.append(f"  time-to-serving         {est([r['tts_s'] for r in rows])}")
    L.append(f"    = critical_platform   {est([r['crit_platform_s'] for r in rows])}")
    L.append(f"    + critical_agent      {est([r['crit_agent_s'] for r in rows])}")
    L.append(f"    (overlap              {est([r['overlap_s'] for r in rows])})")
    L.append(f"  first-attempt LIVENESS  {k_first}/{n} = {fr}% (Wilson 95% CI {flo}-{fhi})")
    L.append(f"  eventual (w/ repair)    {k_reach}/{n} = {rr}% (Wilson 95% CI {rlo}-{rhi})")
    cvs = [_g(rec, "success_oracle", "content_verified") for rec in recs]
    n_unver = sum(1 for x in cvs if x is False)
    n_ver = sum(1 for x in cvs if x is True)
    if n_unver:
        bad = sorted({_g(rec, "success_oracle", "note") for rec in recs
                      if _g(rec, "success_oracle", "content_verified") is False and _g(rec, "success_oracle", "note")})
        L.append(f"  content-verified        WARN {n_unver}/{n} served <500 but look like an infra/default page "
                 f"({', '.join(bad)}); FALSE-SUCCESS flag (M1, off-clock oracle separate from t1); {n_ver}/{n} verified app content")
    elif n_ver:
        L.append(f"  content-verified        {n_ver}/{n} (served root is real app content, not an infra/default "
                 "page; off-clock success oracle, separate from the liveness clock t1)")
    L.append(f"  agent session (whole)   {est([r['agent_end_s'] for r in rows])}   "
             f"of which after serving {est([r['after_s'] for r in rows])}")
    L.append(f"  steps (LLM calls)       {est([r['steps'] for r in rows], unit='')}   (whole session)")
    L.append(f"  agent API cost          {est([r['cost_usd'] for r in rows], unit=' usd')}   (whole session)")
    cap_dcis = [d for d in (_g(r, "capability", "normalized", "dci_partial") for r in recs) if d is not None]
    if cap_dcis:
        cap_axes = next((_g(r, "capability", "normalized", "axes") for r in recs
                         if _g(r, "capability", "normalized", "axes")), None)
        L.append(f"  capability C (DCI)      {est(cap_dcis, unit='')}   axes={cap_axes}  "
                 f"(delivered-under-residency vs frozen reference, off-clock geomean; C11/C14)")
        net_rtts = [_g(r, "capability", "disclosure", "network", "rtt_ms_avg") for r in recs]
        net_rtts = [x for x in net_rtts if isinstance(x, (int, float))]
        if net_rtts:
            L.append(f"    network axis (C17)    VM-to-VM private RTT {est(net_rtts, unit='ms')}  "
                     f"(measured on a multi-VM operation; throughput a disclosed NIC constraint)")
        _ce = next((_g(r, "capability", "error") for r in recs
                    if _g(r, "capability") and not _g(r, "capability", "ok")), None)
        L.append("  capability C            " + (f"attempted, no vector ({_ce}); off-clock, non-fatal"
                 if _ce else "DEFERRED (raw-infra micro-probes over SSH; run with --capability)"))
    eff = gold.part3_provision(recs)
    if eff:
        fr = est([pr["floor_ratio"] for pr in eff["per_run"]], unit="x")
        fs = eff.get("floor_sensitivity", {})
        L.append(f"  efficiency (Part 3)     floor-ratio {fr}  "
                 f"(F_C={eff['F_C_s']}s min-platform floor, n={eff.get('floor_estimated_from_n')}; "
                 f"bracket [{eff['bracket_low_s']}, {eff['bracket_high_s']}]s = x{eff['bracket_ratio']}; "
                 f"selection-excess 0, 1-op suite; gold {eff.get('gold_version')})")
        L.append(f"    competitive ratio is an INTERVAL [M/best, M/F_C], not a point; per-run "
                 f"{[pr.get('competitive_ratio_interval') for pr in eff['per_run']]}")
        if fs:
            L.append(f"    floor sensitivity: bracket ratio {fs.get('bracket_ratio_at_F_C_plus')}..{fs.get('bracket_ratio_at_F_C_minus')} "
                     f"at F_C(1+/-{fs.get('epsilon')}) (denominator leverage; refutable + versioned gold)")
    else:
        L.append("  efficiency (Part 3)     DEFERRED (no run carries an acspeed split yet)")
    L.append("  (reading)               efficiency + capability are WORKLOAD-RELATIVE (Part 4): an axis "
             "ceiling lowers measured efficiency only for workloads bound by that axis; each figure carries "
             "its axis set, never a bare cross-workload verdict")
    L.append("  cold/warm               POOLED above; per-run warmth shown below (real stratification = C8)")
    L.append("")
    L.append("TABLE A  (per run, seconds; t-serve = t1 - t0 by EXTERNAL poll; agt_end = agent's own finish)")
    hdr = ["run", "outcome", "rpr", "flavor", "t-serve", "agt_end", "platf_s", "agent_s", "boot_s",
           "steps", "tok", "$", "warm", "dead"]
    L.append("  " + "  ".join(f"{h:>8}" for h in hdr))
    for r in rows:
        cells = [r["run"], (r["outcome"] or "")[:12],
                 (r["repair_prompts"] if r["repair_prompts"] is not None else "-"),
                 (r["flavor"] or "-"), r["tts_s"], r["agent_end_s"], r["crit_platform_s"],
                 r["crit_agent_s"], r["boot_s"], r["steps"], r["out_tokens"], r["cost_usd"],
                 (str(r["warmth"] or "-"))[:8], r["url_dead"]]
        L.append("  " + "  ".join(f"{str(c):>8}" for c in cells))
    flagged = [r["run"] for r in rows if r.get("first_poll")]
    if flagged:
        L.append(f"  ** runs {flagged}: URL served on the very FIRST poll; check it was not a leftover deployment **")
    shots = [(r["run"], r["shot"]) for r in rows if r.get("shot")]
    if shots:
        L.append("  screenshots (optional, human-facing evidence; NOT scored, not part of any metric):")
        for run, path in shots:
            L.append(f"    run {run}: {path}")
    L.append("")
    L.append("TABLE B  (keyed to the paper)")
    L.append(f"  Part 1 split (seconds)  platform {[r['crit_platform_s'] for r in rows]}  "
             f"agent {[r['crit_agent_s'] for r in rows]}  overlap {[r['overlap_s'] for r in rows]}  "
             f"(trace clipped at t1)")
    if cap_dcis:
        net_rtts_b = [x for x in (_g(r, "capability", "disclosure", "network", "rtt_ms_avg") for r in recs)
                      if isinstance(x, (int, float))]
        net_note = (f"VM-to-VM private RTT avg {[round(x, 3) for x in net_rtts_b]}ms (C17, multi-VM)"
                    if net_rtts_b else "network N/A for single-VM operations (C17/C18)")
        L.append(f"  Part 1 capability C     DCI {[round(d, 3) for d in cap_dcis]} (sysbench/STREAM/fio "
                 f"off-clock on the deploy's own VM; {net_note})")
    else:
        L.append(f"  Part 1 capability C     DEFERRED (sysbench/STREAM/fio off-clock; network is a "
                 f"VM-to-VM axis, scored only for multi-VM operations, C17/C18)")
    L.append(f"  Part 1 portable proxy   steps {[r['steps'] for r in rows]}")
    pred = (_g(recs[0], "serving", "predicate") or "first response with HTTP status < 500")
    L.append(f"  Part 2 operation        PROVISION; end-signal = the app answers: {pred}; "
             f"external concurrent poll, never self-report; caught by {[r['caught_by'] for r in rows]}")
    if eff:
        L.append(f"  Part 3 efficiency       floor-ratio {[round(pr['floor_ratio'], 2) for pr in eff['per_run']]}  "
                 f"(F_C {eff['F_C_s']}s = min-observed platform floor; best-achieved {eff['best_achieved_s']}s; "
                 f"bracket width {eff['bracket_width_s']}s = x{eff['bracket_ratio']}); "
                 f"exec-excess {[pr['execution_excess_s'] for pr in eff['per_run']]}s; selection 0 (1-op)")
    else:
        L.append(f"  Part 3 efficiency       DEFERRED (reference-optimal gold not authored)")
    cpts = [(r.get("flavor") or "?", r["tts_s"], _g(rec, "cost_run_rate", "all_in_hourly_usd"),
             _g(rec, "cost_run_rate", "monthly_usd"))
            for rec, r in zip(recs, rows)
            if r.get("tts_s") and _g(rec, "cost_run_rate", "all_in_hourly_usd") is not None]
    if cpts:
        cruns = [weighting.Run(label=f"{fl}#{i}", time=t, cost=c) for i, (fl, t, c, _m) in enumerate(cpts)]
        eff_flavors = [r.label for r in weighting.pareto_frontier(cruns)]
        L.append(f"  Part 4 cost frontier    run-rate $/hr {[round(c, 4) for _f, _t, c, _m in cpts]} "
                 f"(monthly {[round(m, 2) if m else None for _f, _t, _c, m in cpts]}); "
                 f"(wall,$/hr) Pareto-efficient {eff_flavors}  (dated public list, egress separate)")
        fxs = [_g(rec, "cost_run_rate", "fx") for rec in recs if _g(rec, "cost_run_rate", "fx")]
        if fxs:
            fx = fxs[0]
            L.append(f"    (FX: $/hr converted from the native {fx['native_currency']} list price at "
                     f"{fx['native_currency']}->{fx['reporting_currency']} {fx['rate']} on {fx['rate_date']}, "
                     f"{fx['source']})")
    else:
        usage = next((_g(rec, "cost_run_rate") for rec in recs
                      if (_g(rec, "cost_run_rate") or {}).get("kind") == "usage"), None)
        if usage:
            svc = usage.get("service", "usage-metered")
            pts = "  ".join(f"{p['label']}->${p['usd_per_month']}/mo" for p in (usage.get("schedule") or []))
            L.append(f"  Part 4 cost frontier    USAGE-METERED ({svc}): per-usage schedule (requests/mo): {pts}")
            for a in (usage.get("assumptions") or [])[:1]:
                L.append(f"    (assumption: {a}; egress + provisioned floor folded, active per-second compute listed separately)")
        else:
            L.append(f"  Part 4 cost frontier    DEFERRED (run --cost: standing hourly run-rate, dated public list)")
    te = next((_g(rec, "cost_run_rate", "traffic_estimate") for rec in recs
               if _g(rec, "cost_run_rate", "traffic_estimate")), None)
    if te:
        L.append(f"  Part 4 cost by traffic  est total $/mo: low ${te.get('low')} (10k) / "
                 f"medium ${te.get('medium')} (500k) / high ${te.get('high')} (10M req/mo)")
    L.append(f"  Part 5 speed+liveness   time-to-serving {[r['tts_s'] for r in rows]} s; "
             f"first-attempt {k_first}/{n} = {fr}% (Wilson CI)")
    L.append("=" * W)
    return "\n".join(L)


def report(out_dir: str) -> str | None:
    """Load the run records in out_dir, render the tables, write tables.json + tables.txt beside them,
    and return the rendered text (None if there are no acspeed-schema records yet)."""
    recs = load_runs(out_dir)
    if not recs:
        return None
    rows = [per_run_row(r) for r in recs]
    task = recs[0].get("task", "?")
    bundle = {"generated": datetime.now(timezone.utc).isoformat(),
              "cloud": recs[0].get("cloud", "?"), "task": task, "runs": rows,
              "part3_efficiency": gold.part3_provision(recs)}
    with open(os.path.join(out_dir, "tables.json"), "w") as fh:
        json.dump(bundle, fh, indent=2, default=str)
    txt = text_render(rows, recs, task)
    with open(os.path.join(out_dir, "tables.txt"), "w") as fh:
        fh.write(txt + "\n")
    return txt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(
        os.environ.get("ACSPEED_DATA") or os.path.expanduser("~/.acspeed"), "_measurements"))
    a = ap.parse_args()
    txt = report(a.dir)
    if txt is None:
        print(f"no acspeed-schema run??.json in {a.dir}")
    else:
        print(txt)
        print(f"\n[written] {os.path.join(a.dir, 'tables.txt')}  +  tables.json")


if __name__ == "__main__":
    main()
