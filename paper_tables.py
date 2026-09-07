#!/usr/bin/env python3
"""Emit the LaTeX bodies for the paper's Part 5 result tables from the published results tree.

The paper anonymizes clouds and never names the validation instance, so redu is excluded here by
construction, not by a flag. Every number is read from a run record; nothing is typed by hand, so
re-running this after a batch is the whole update procedure.

    python3 paper_tables.py            # print the table bodies
    python3 paper_tables.py --check    # recompute and diff against what the .tex currently holds

Cells are taken from results/<cloud>/<cloud>-<tier>/README.md (the published rows, i.e. the fair
set) and joined to the staging records by SESSION UUID, never by run number: several cells number
their rows 1..n for display while the staging ids are non-contiguous, so a number join silently
reads the wrong runs.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from acspeed import gold, repro, weighting  # noqa: E402
from build_tables import wilson  # noqa: E402

STAGING = "/home/milos/Desktop/tests_lib/umami/acspeed-results"
RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

# The paper's anonymization. Kept here, deliberately NOT in the .tex, so the tables can be
# regenerated without the key leaking into the manuscript.
ANON = OrderedDict([("aws", "A"), ("gcp", "B"), ("azure", "C")])

# Tier label -> (stage suffix, the paper's tier name). Easy has no regime; Medium is crossed with
# the online/disclosed regime, which is what medium-a and medium-b are.
TIERS = [
    ("easy", "", "Easy"),
    ("medium-a", "-medium-a", "Medium (online)"),
    ("medium-b", "-medium-b", "Medium (disclosed)"),
]

# The normalizer for G_X. Any fixed cloud works: the ranking of G_X is invariant to the choice
# (Fleming and Wallace 1986), which is the whole reason the companion exists.
GX_NORMALIZER = "aws"

SESS_RE = re.compile(r"^\|\s*\[\d+\]\(sessions/([0-9a-f-]+)\.jsonl\)", re.M)


def _stage_dir(cloud: str, suffix: str) -> str:
    return os.path.join(STAGING, cloud + suffix)


def _records_by_session(stage: str) -> dict:
    out = {}
    for path in glob.glob(os.path.join(stage, "run*.json")):
        rec = json.load(open(path))
        for rnd in rec.get("rounds") or []:
            if rnd.get("session"):
                out[rnd["session"]] = rec
    return out


def published_records(cloud: str, suffix: str, tier_key: str) -> list:
    """The published rows of a cell, resolved to run records through the session UUID."""
    cell = os.path.join(RESULTS, cloud, f"{cloud}-{tier_key}", "README.md")
    uuids = SESS_RE.findall(open(cell).read())
    index = _records_by_session(_stage_dir(cloud, suffix))
    missing = [u for u in uuids if u not in index]
    if missing:
        raise SystemExit(f"{cloud}-{tier_key}: {len(missing)} published rows have no run record")
    return [index[u] for u in uuids]


# ---------------------------------------------------------------------------------------------
# Per-run quantities
# ---------------------------------------------------------------------------------------------

def _legs(rec: dict):
    """Every timed leg of the task: the deploy, then each scored operation, then the durability
    cycles. Each contributes a critical-path makespan and its platform/agent split."""
    legs = [rec.get("split")]
    tier_run = rec.get("tier_run") or {}
    for op in tier_run.get("operations") or []:
        legs.append(op.get("split"))
    for cyc in (tier_run.get("durability") or {}).get("cycles") or []:
        legs.append(cyc.get("split"))
    return [l for l in legs if isinstance(l, dict)]


def task_M(rec: dict) -> float | None:
    """Part 4's per-task total M: the critical-path sum over the task's legs. Every leg contributes
    its own makespan, which already includes the platform-owned boot span up to the readiness
    signal, so M, the platform/agent columns and the overlap stay on one scale and
    ``M = platform + agent + overlap`` holds by construction. Time-to-serving is NOT substituted
    for the deploy leg: it is the externally polled signal Part 3 uses for the floor ratio, and
    mixing the two bases would make this table's columns fail to add up."""
    legs = _legs(rec)
    if not legs:
        return None
    return sum(l.get("makespan_s") or 0.0 for l in legs)


def task_split(rec: dict):
    """Platform, agent and overlap critical-path seconds summed over the task's legs."""
    legs = _legs(rec)
    if not legs:
        return None, None, None
    plat = sum(l.get("critical_platform_s") or 0.0 for l in legs)
    agent = sum(l.get("critical_agent_s") or 0.0 for l in legs)
    # Each leg's makespan is cp + ca + any held-out idle in the window (the idle the owner split
    # attributes to neither lane). Carrying it explicitly is what makes M add up.
    idle = sum(max(0.0, (l.get("makespan_s") or 0.0)
                   - (l.get("critical_platform_s") or 0.0)
                   - (l.get("critical_agent_s") or 0.0)) for l in legs)
    return plat, agent, idle


def check_identity(cells: list) -> list:
    """Part 1's spine, per leg: makespan = critical_platform + critical_agent + held-out idle.
    A table whose own columns do not add up is not publishable, so this runs before emission."""
    bad = []
    for c in cells:
        if None in (c["M_mean"], c["platform"], c["agent"]):
            bad.append(f"{c['anon']} {c['tier']}: missing a component")
            continue
        lhs = c["M_mean"]
        rhs = c["platform"] + c["agent"] + (c["idle"] or 0.0)
        if abs(lhs - rhs) > 0.5:
            bad.append(f"{c['anon']} {c['tier']}: M={lhs:.1f} but platform+agent+idle={rhs:.1f} "
                       f"(off by {lhs - rhs:+.1f}s)")
    return bad


def arch_class(rec: dict) -> str:
    """The architecture class the agent selected, from the priced components. This is what the
    Table 5.1 caption means by stratification; it is a run property, not a cloud property."""
    crr = rec.get("cost_run_rate") or {}
    names = {c.get("name", "") for c in crr.get("components") or []}
    svc = (crr.get("service") or "").lower()
    has_db = any("rds" in n or "sql" in n or "postgres" in n or "relationaldatabase" in n
                 for n in names)
    if any("fargate" in n for n in names):
        base = "serverless container"
    elif any("containerservice" in n for n in names) or "container app" in svc:
        base = "managed container"
    elif "cloud run" in svc:
        base = "serverless container"
    elif any("app-service" in n or "appservice" in n for n in names):
        base = "managed PaaS"
    elif any(n.startswith("compute") for n in names):
        base = "VM"
    else:
        base = "managed container"
    return base + (" + managed DB" if has_db else "")


# ---------------------------------------------------------------------------------------------
# Cell aggregation
# ---------------------------------------------------------------------------------------------

def cell(cloud: str, suffix: str, tier_key: str, tier_name: str) -> dict:
    recs = published_records(cloud, suffix, tier_key)
    Ms = [m for m in (task_M(r) for r in recs) if m]
    splits = [task_split(r) for r in recs]
    plat = [p for p, _, _ in splits if p is not None]
    agent = [a for _, a, _ in splits if a is not None]
    over = [o for _, _, o in splits if o is not None]

    eff = gold.part3_provision(recs) or {}
    per = eff.get("per_run") or []
    floor = [p["floor_ratio"] for p in per]
    best = [p["best_ratio"] for p in per]
    sel = [p["selection_excess_s"] for p in per]
    exc = [p["execution_excess_s"] for p in per]

    live_k = sum(1 for r in recs if r.get("first_attempt_success"))
    pct, lo, hi = wilson(live_k, len(recs))

    classes = {}
    for r in recs:
        k = arch_class(r)
        classes[k] = classes.get(k, 0) + 1
    classes = OrderedDict(sorted(classes.items(), key=lambda kv: (-kv[1], kv[0])))
    m_est = repro.bootstrap_ci(Ms) if len(Ms) > 1 else None

    return {
        "cloud": cloud, "anon": ANON[cloud], "tier": tier_name,
        "n": len(recs), "n_gold": len(per),
        "M_mean": sum(Ms) / len(Ms) if Ms else None,
        "M_lo": getattr(m_est, "lo", None), "M_hi": getattr(m_est, "hi", None),
        "platform": sum(plat) / len(plat) if plat else None,
        "agent": sum(agent) / len(agent) if agent else None,
        "idle": sum(over) / len(over) if over else None,
        "live_k": live_k, "live_pct": pct, "live_lo": lo, "live_hi": hi,
        "ratio_lo": sum(best) / len(best) if best else None,
        "ratio_hi": sum(floor) / len(floor) if floor else None,
        "sel": sum(sel) / len(sel) if sel else None,
        "exec": sum(exc) / len(exc) if exc else None,
        "arch": classes,
        "cost": [(r.get("cost_run_rate") or {}) for r in recs],
    }


def all_cells() -> list:
    return [cell(c, sfx, key, name) for c in ANON for key, sfx, name in TIERS]


# ---------------------------------------------------------------------------------------------
# LaTeX emission
# ---------------------------------------------------------------------------------------------

def _n(x, d=0):
    return "---" if x is None else (f"{x:,.{d}f}" if d else f"{x:,.0f}")


SHORT = {
    "serverless container + managed DB": "svc+db", "serverless container": "svc",
    "managed container + managed DB": "mc+db", "managed container": "mc",
    "managed PaaS + managed DB": "paas+db", "managed PaaS": "paas",
    "VM + managed DB": "vm+db", "VM": "vm",
}


def _arch_cell(classes) -> str:
    """The architecture strata actually observed in the cell, with counts. A cell with more than
    one stratum is a selection result, not noise, so it is shown rather than collapsed."""
    return ", ".join(f"{SHORT.get(k, k)} $\\times${v}" for k, v in classes.items())


def table_51(cells: list) -> str:
    rows = []
    for c in cells:
        rows.append(
            f"{c['anon']} & {c['tier']} & {c['n']} & {_n(c['M_mean'])} "
            f"[{_n(c['M_lo'])}, {_n(c['M_hi'])}] & "
            f"{_n(c['platform'])} / {_n(c['agent'])} / {_n(c['idle'])} & "
            f"{c['live_k']}/{c['n']} [{c['live_lo']:.0f}, {c['live_hi']:.0f}]\\% & "
            f"$[{c['ratio_lo']:.2f},\\ {c['ratio_hi']:.2f}]$ & "
            f"{_n(c['sel'], 1)} / {_n(c['exec'], 1)} \\\\")
    rows.append(r"\multicolumn{8}{l}{\emph{Hard tier: not run; no cell exists on any cloud.}} \\")
    return "\n".join(rows)


def table_52(cells: list) -> str:
    by_cloud = {}
    for c in cells:
        by_cloud.setdefault(c["cloud"], {})[c["tier"]] = c["M_mean"]
    totals = {cl: weighting.suite_total(m) for cl, m in by_cloud.items()}
    ref = by_cloud[GX_NORMALIZER]
    gx = {cl: weighting.geomean_ratio(m, ref) for cl, m in by_cloud.items()}

    e_rank = sorted(totals, key=lambda c: totals[c])
    g_rank = sorted(gx, key=lambda c: gx[c])
    agree = "yes" if e_rank == g_rank else "no"

    runs = []
    for c in cells:
        if c["tier"] != "Easy":
            continue
        rates = [_hourly(x) for x in c["cost"]]
        rates = [r for r in rates if r is not None]
        if rates:
            runs.append(weighting.Run(label=c["cloud"], time=totals[c["cloud"]],
                                      cost=sum(rates) / len(rates)))
    front = {r.label for r in weighting.pareto_frontier(runs)} if runs else set()

    rows = []
    for cl in ANON:
        loo = "n/a (single pair)"
        others = [o for o in ANON if o != cl]
        if others:
            checks = [weighting.leave_one_out(by_cloud[cl], by_cloud[o])["robust"] for o in others]
            loo = "yes" if all(checks) else "no"
        rows.append(f"{ANON[cl]} & {_n(totals[cl])} & {gx[cl]:.3f} & {agree} & {loo} & "
                    f"{'yes' if cl in front else 'no'} \\\\")
    return "\n".join(rows)


def _hourly(crr: dict):
    if not crr:
        return None
    if crr.get("kind") == "standing" and crr.get("monthly_usd") is not None:
        return crr["monthly_usd"] / 730.0
    te = crr.get("traffic_estimate") or {}
    return te["low"] / 730.0 if te.get("low") is not None else None


TEX = ("/home/milos/Desktop/redu-webservice/internal/proof-comparison-2026-08/"
       "PAPER/combined_p5body.tex")


def check_against_tex(cells: list) -> list:
    """Every generated row must appear verbatim in the paper. Catches the paper drifting from the
    results tree, in either direction."""
    tex = open(TEX).read()
    missing = []
    for body in (table_51(cells), table_52(cells)):
        for row in body.splitlines():
            row = row.strip()
            if not row or row.startswith("\\multicolumn"):
                continue
            if row not in tex:
                missing.append(row)
    return missing


def main() -> None:
    cells = all_cells()
    if "--check" in sys.argv:
        problems = check_identity(cells)
        drift = check_against_tex(cells)
        for b in problems:
            print("IDENTITY: " + b)
        for d in drift:
            print("DRIFT (row not in paper): " + d)
        if problems or drift:
            raise SystemExit(1)
        print("OK: identity holds and every generated row is present verbatim in Part 5.")
        return
    problems = check_identity(cells)
    if problems:
        print("IDENTITY CHECK FAILED, not emitting:", file=sys.stderr)
        for b in problems:
            print("  " + b, file=sys.stderr)
        raise SystemExit(1)
    print("% ---- Table 5.1 body (generated by paper_tables.py, do not hand-edit) ----")
    print(table_51(cells))
    print()
    print()
    print("% ---- Table 5.1 architecture strata (for the note under the table) ----")
    for c in cells:
        print(f"%   {c['anon']} {c['tier']}: {_arch_cell(c['arch'])}")
    print()
    print("% ---- Table 5.2 body (generated by paper_tables.py, do not hand-edit) ----")
    print(table_52(cells))
    print()
    print(f"% anonymization: {', '.join(f'{v}={k}' for k, v in ANON.items())}"
          f"; G_X normalizer = {GX_NORMALIZER}")


if __name__ == "__main__":
    main()
