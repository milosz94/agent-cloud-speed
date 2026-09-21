#!/usr/bin/env python3
"""Emit the LaTeX bodies for the paper's Part 5 result tables from the published results tree.

The paper names the three clouds it reports and does not report the validation instance, so redu is
excluded here by construction, not by a flag. Every number is read from a run record; nothing is typed by hand, so
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
import random
from fractions import Fraction
from itertools import combinations_with_replacement
from math import factorial
import re
import sys
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from acspeed import gold, repro, weighting  # noqa: E402
from build_tables import wilson  # noqa: E402

# Raw per-run records. The records behind the PUBLISHED cells ship in the artifact, under
# results/<cloud>/<cloud>-<tier>/records/, and _record_dirs falls back to them, so these tables
# regenerate with ACSPEED_STAGING unset. Point ACSPEED_STAGING at your own acspeed-results tree to
# regenerate them from your own runs instead.
STAGING = os.environ.get("ACSPEED_STAGING") or os.path.join(
    (os.environ.get("ACSPEED_DATA") or os.path.expanduser("~/.acspeed")), "_staging")
RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")

# The paper's anonymization. Kept here, deliberately NOT in the .tex, so the tables can be
# regenerated without the key leaking into the manuscript.
# Providers are NAMED. The paper published anonymized clouds until 2026-09-07; the benchmark is
# released open-source with the runs and transcripts attached, so anonymity was decorative once a
# reader could map a number to a provider in a minute. All three providers' terms permit named
# publication (BENCHMARK-TERMS.md); what they ask for is a disclosure that can be replicated.
ANON = OrderedDict([("aws", "AWS"), ("gcp", "GCP"), ("azure", "Azure")])

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


def _record_dirs(cloud: str, suffix: str, tier_key: str) -> list:
    """Where per-run records may live, in priority order.

    The private staging tree first, so a re-run after a fresh batch picks up new runs; then the
    records PUBLISHED beside the cell table, which is what makes these tables regenerable by anyone
    who has only the artifact. Before the published copy existed a reader could see every number in
    Part 5 and still not recompute one, which is the gap tools/publish_records.py closes."""
    return [
        os.path.join(STAGING, cloud + suffix),
        os.path.join(RESULTS, cloud, f"{cloud}-{tier_key}", "records"),
    ]


def _records_by_session(stages) -> dict:
    if isinstance(stages, str):
        stages = [stages]
    out = {}
    # Later directories must not override earlier ones: staging wins where it has the run.
    for stage in reversed(list(stages)):
        for path in glob.glob(os.path.join(stage, "run*.json")):
            rec = json.load(open(path))
            for rnd in rec.get("rounds") or []:
                if rnd.get("session"):
                    out[rnd["session"]] = rec
    return out


def published_uuids(cloud: str, tier_key: str) -> list:
    """The session UUIDs of a cell's published rows, in printed order. One per published run."""
    cell = os.path.join(RESULTS, cloud, f"{cloud}-{tier_key}", "README.md")
    return SESS_RE.findall(open(cell).read())


def published_records(cloud: str, suffix: str, tier_key: str) -> list:
    """The published rows of a cell, resolved to run records through the session UUID."""
    uuids = published_uuids(cloud, tier_key)
    index = _records_by_session(_record_dirs(cloud, suffix, tier_key))
    missing = [u for u in uuids if u not in index]
    if missing:
        raise SystemExit(f"{cloud}-{tier_key}: {len(missing)} published rows have no run record "
                         f"(looked in: {', '.join(_record_dirs(cloud, suffix, tier_key))})")
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
    signal, so M, the platform/agent columns and the third term stay on one scale and
    ``M = platform + agent + other`` holds by construction (the paper's column is named *other*). Time-to-serving is NOT substituted
    for the deploy leg: it is the externally polled signal Part 3 uses for the floor ratio, and
    mixing the two bases would make this table's columns fail to add up."""
    legs = _legs(rec)
    if not legs:
        return None
    return sum(l.get("makespan_s") or 0.0 for l in legs)


def task_split(rec: dict):
    """Platform, agent and other critical-path seconds summed over the task's legs."""
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
    # The vocabulary changed under this function twice: the CloudTrail pricer emits the CREATE EVENT's
    # noun (dbinstance, containerservice, vcpu, instances) where the inventory pricer emitted a
    # hand-written label (compute:fargate-vcpu, compute:rds). Measured 2026-09-07: every AWS run fell
    # through to the catch-all and every managed database went unseen, so Table 5.1's strata were wrong
    # for the whole cloud. Both vocabularies are matched here, and an UNRECOGNIZED bundle now says so
    # instead of silently reporting "managed container".
    has_db = any(("rds" in n or "sql" in n or "postgres" in n or "relationaldatabase" in n
                  or n == "dbinstance") for n in names)
    # The SERVICE field decides before any component heuristic, because Azure Container Apps and Cloud
    # Run emit byte-identical components (compute-active-cpu, compute-active-mem, requests) and differ
    # only by name. Classifying on components alone put Azure in the Cloud Run bucket.
    if "container app" in svc:
        base = "managed container"
    elif "cloud run" in svc:
        base = "serverless container"
    elif any("fargate" in n for n in names) or {"vcpu", "gb"} <= names:
        base = "serverless container"          # Fargate: billed per vCPU-hour and GB-hour
    elif any("containerservice" in n for n in names):
        base = "managed container"
    elif any("compute-active-cpu" in n or "compute-active-mem" in n for n in names):
        base = "serverless container"          # per active CPU and memory, no named service
    elif any("app-service" in n or "appservice" in n for n in names):
        base = "managed PaaS"
    elif any(n == "instances" or n.startswith("compute:") for n in names):
        base = "VM"
    elif not names:
        base = "unpriced"
    else:
        base = "unclassified"                  # visible, never a silent default
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
        "Ms": Ms,          # per-run makespans: the resampling unit for the suite bootstrap
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
    return "\n".join(rows)


# ------------------------------------------------------------------------------------------------
# Uncertainty on the suite summaries (Part 4, the per-task bootstrap)
# ------------------------------------------------------------------------------------------------

BOOT_B = 10000
BOOT_SEED = 20260907


def check_easy_pairing_alignment(cells: list) -> list:
    """Position i of an Easy cell's ``Ms`` and of its ``cost`` must be the SAME RUN.

    ``_suite_replicates`` pairs the cost draw to the Easy-cell makespan draw BY INDEX. A shift between
    the two lists would pair one run's time with another run's cost on every replicate, silently.

    NOTHING ELSE DETECTS THAT. Pairing every makespan with a randomly relabelled run's cost moves the
    frontier frequency by less than the bootstrap's own Monte Carlo noise (measured: 7.797 against
    7.739), so the paired-versus-unpaired sensitivity Part 5 reports cannot catch it, and neither can a
    length guard, because two lists of equal length can still be in different orders. This walks both
    lists back to the run records and to the published session UUIDs.

    The live hazard is concrete: ``cell()`` builds ``Ms`` with a falsy filter and ``cost`` without one,
    so a single unmeasured makespan shifts every index after it.
    """
    problems, tiers = [], {name: (key, sfx) for key, sfx, name in TIERS}
    checked = set()
    for c in cells:
        if c["tier"] != EASY_TIER:
            continue
        cloud, (key, sfx) = c["cloud"], tiers[c["tier"]]
        checked.add(cloud)
        uuids = published_uuids(cloud, key)
        recs = published_records(cloud, sfx, key)
        where = f"{cloud} {c['tier']}"
        # len(recs) == len(uuids) is NOT tested: published_records returns one record per uuid or
        # raises, so that term can never fail and a mutation test confirmed it kills no test.
        if not (len(recs) == len(c["Ms"]) == len(c["cost"])):
            problems.append(f"{where}: {len(recs)} records, {len(c['Ms'])} makespans, "
                            f"{len(c['cost'])} cost records. A dropped makespan shifts Ms against "
                            f"cost and the pairing is no longer run-for-run")
            continue
        if len(recs) < 2:
            problems.append(f"{where}: {len(recs)} published row(s), so no ordering was verified "
                            f"and a green result here proves nothing about the pairing")
            continue
        # DISTINCTNESS. This replaces an assertion that could not fail. An earlier version asserted
        # that recs[i] carries uuids[i]; published_records returns index[uuids[i]] and index is keyed
        # by that very session, so it compared a value to the key it was fetched by and passed under
        # every possible row order, including reversed and shuffled. Verified 2026-09-21, and an
        # independent mutation test found deleting it left the whole suite green.
        #
        # Scope, stated narrowly: these catch the DUPLICATED form of a bad row-to-record join, where
        # one run serves two published rows. They do not catch the PERMUTED form, where two rows swap
        # records; that moves Ms and cost together so the pairing stays correct, and tools/audit_fields.py
        # catches it as a field mismatch. The UUID test below is a message refinement of the id() test
        # rather than an independent control: a repeated uuid always yields a repeated record object.
        if len(set(uuids)) != len(uuids):
            problems.append(f"{where}: a session UUID is published twice, so one run supplies two "
                            f"rows and the cell carries a pseudo-replicate")
        if len({id(r) for r in recs}) != len(recs):
            problems.append(f"{where}: two published rows resolve to the SAME run record, so a run is "
                            f"counted twice and another published run has no record of its own")
        for i, r in enumerate(recs):
            if task_M(r) != c["Ms"][i]:
                problems.append(f"{where} position {i}: Ms[{i}]={c['Ms'][i]} is not this run's "
                                f"makespan {task_M(r)}")
            if (r.get("cost_run_rate") or {}) != c["cost"][i]:
                problems.append(f"{where} position {i}: cost[{i}] is not this run's cost record")
            if _hourly(c["cost"][i]) is None:
                problems.append(f"{where} position {i}: no hourly rate, so the paired draw has a "
                                f"makespan with no cost beside it")
    absent = [c for c in ANON if c not in checked]
    if absent:
        problems.append("no Easy cell reached this check for " + ", ".join(absent)
                        + ": the pairing guard ran vacuously and proves nothing")
    return problems


EASY_TIER = "Easy"

# The Easy cell is the one cell whose runs supply BOTH frontier coordinates (the cost coordinate is read
# from it alone; the time coordinate is the suite total over all three tiers). COST_PAIRED resamples whole
# run records there, so a replicate's time and its cost coordinate come off one draw and carry whatever
# within-run association exists between them. False draws the two independently, which is the scheme this
# generator shipped with and the sensitivity Part 5's frontier note reports against.
#
# The independent draw is taken under BOTH schemes and discarded when pairing. That is deliberate, not a
# leftover: it keeps one seeded stream, so the two schemes run on IDENTICAL time replicates. The E_X and
# G_X intervals therefore do not move with this switch, and the two frontier frequencies differ only in
# how the cost coordinate was drawn, which is the comparison the note makes.
COST_PAIRED = True


def _suite_replicates(cells: list, paired: bool = None, seed: int = None):
    """Resample RUNS within each task, recompute E_X, G_X and the frontier on every replicate.

    The suite is fixed by design, so runs are the only resampled unit for a sampling interval: task-level
    sensitivity is carried separately by leave-one-out, never folded in here. Returns per-cloud lists of
    (E_X, G_X) plus the frontier-membership count, i.e. exactly the three quantities Part 4 promises.
    """
    paired = COST_PAIRED if paired is None else paired
    if paired:
        misaligned = check_easy_pairing_alignment(cells)
        if misaligned:
            raise SystemExit("paired cost resampling refuses to run on misaligned cells:\n  "
                             + "\n  ".join(misaligned))
    rnd = random.Random(BOOT_SEED if seed is None else seed)
    by_cloud, rates = {}, {}
    for c in cells:
        by_cloud.setdefault(c["cloud"], {})[c["tier"]] = c["Ms"]
        if c["tier"] == EASY_TIER:
            rates[c["cloud"]] = [_hourly(x) for x in c["cost"]]   # index-aligned with this cell's Ms
    clouds = list(by_cloud)
    ex = {c: [] for c in clouds}
    gx = {c: [] for c in clouds}
    front = {c: 0 for c in clouds}
    for _ in range(BOOT_B):
        means, easy_pick = {}, {}
        for c, tiers in by_cloud.items():
            means[c] = {}
            for t, ms in tiers.items():
                if not ms:
                    means[c][t] = None
                    continue
                pick = rnd.choices(range(len(ms)), k=len(ms))     # same draw count as choices(ms, ...)
                if t == EASY_TIER:
                    easy_pick[c] = pick
                means[c][t] = sum(ms[i] for i in pick) / len(ms)
        tot = {c: weighting.suite_total(m) for c, m in means.items()}
        ref = means[GX_NORMALIZER]
        cost = {}
        for c in clouds:
            rs = [r for r in (rates.get(c) or []) if r is not None]
            if not rs:
                cost[c] = None
                continue
            pick = rnd.choices(range(len(rs)), k=len(rs))         # taken under both schemes, see above
            if paired:
                easy = by_cloud[c].get(EASY_TIER) or []
                if c not in easy_pick or len(rs) != len(easy):
                    raise SystemExit(                              # visible, never a silent mispairing
                        f"paired cost resampling needs one priced run per Easy-cell makespan on {c}: "
                        f"{len(rs)} priced rates against {len(easy)} makespans")
                pick = easy_pick[c]
            cost[c] = sum(rs[i] for i in pick) / len(rs)
        runs = [weighting.Run(label=c, time=tot[c], cost=cost[c]) for c in clouds if cost[c] is not None]
        on = {r.label for r in weighting.pareto_frontier(runs)} if runs else set()
        for c in clouds:
            ex[c].append(tot[c])
            gx[c].append(weighting.geomean_ratio(means[c], ref))
            if c in on:
                front[c] += 1
    return ex, gx, front


EXACT_SUBJECT = "azure"          # the one cloud whose membership reduces to a cost comparison here
EXACT_FASTER = "gcp"             # faster than the subject in every replicate drawn
EXACT_DEARER = "aws"             # can never be cheaper than the subject, by arithmetic


def _mean_law(vals):
    """Exact law of the bootstrap mean of ``vals``: n iid uniform draws with replacement.

    The rates take few distinct values, so the resampled mean is a finite multinomial and its law
    is a set of exact rationals. Floats are converted with Fraction(x), which is the stored binary
    value exactly, not an approximation of it.
    """
    n = len(vals)
    counts = {v: vals.count(v) for v in set(vals)}
    distinct = sorted(counts)
    total = Fraction(n ** n)
    law = {}
    for combo in combinations_with_replacement(range(len(distinct)), n):
        mult = [combo.count(i) for i in range(len(distinct))]
        ways = factorial(n)
        for m in mult:
            ways //= factorial(m)
        w = ways
        for i, m in enumerate(mult):
            w *= counts[distinct[i]] ** m
        mean = sum(Fraction(distinct[i]) * m for i, m in enumerate(mult)) / n
        law[mean] = law.get(mean, Fraction(0)) + Fraction(w) / total
    return law


def frontier_exact(cells: list, replicates: int = 200000) -> dict:
    """Azure's frontier-membership probability in closed form, with no sampling at all.

    Two conditions collapse the two-coordinate comparison to a one-coordinate one. Both are
    CHECKED here, never assumed, because both are properties of this wave rather than of the
    estimator and must not be carried forward silently into another one.

      A, arithmetic. AWS's cheapest Easy run is dearer than Azure's dearest, so no resample can
         put AWS's cost mean at or below Azure's and AWS can never dominate Azure.
      B, empirical. GCP's suite total fell below Azure's in every replicate drawn, so whether GCP
         dominates Azure turns on the cost coordinate alone.

    Given A and B, Azure is non-dominated exactly when GCP's resampled cost mean exceeds Azure's,
    and that probability is a rational number over the finite law of each cell's resampled mean.

    Returns the probability, the two condition checks, and the bootstrap's own estimate beside it.
    Raises SystemExit if either condition fails.
    """
    by_cloud, rates = {}, {}
    for c in cells:
        by_cloud.setdefault(c["cloud"], {})[c["tier"]] = c["Ms"]
        if c["tier"] == EASY_TIER:
            rates[c["cloud"]] = [r for r in (_hourly(x) for x in c["cost"]) if r is not None]

    dear_min, subj_max = min(rates[EXACT_DEARER]), max(rates[EXACT_SUBJECT])
    if not dear_min > subj_max:
        raise SystemExit(
            f"condition A fails: {EXACT_DEARER}'s cheapest Easy run {dear_min:.6f} is not dearer "
            f"than {EXACT_SUBJECT}'s dearest {subj_max:.6f}, so {EXACT_DEARER} can dominate "
            f"{EXACT_SUBJECT} and the comparison no longer reduces to cost")

    rnd = random.Random(BOOT_SEED)
    slower = 0
    for _ in range(replicates):
        means = {c: {t: (sum(rnd.choices(ms, k=len(ms))) / len(ms)) if ms else None
                     for t, ms in tiers.items()}
                 for c, tiers in by_cloud.items()}
        if weighting.suite_total(means[EXACT_FASTER]) > weighting.suite_total(means[EXACT_SUBJECT]):
            slower += 1
    if slower:
        raise SystemExit(
            f"condition B fails: {EXACT_FASTER} was slower than {EXACT_SUBJECT} in {slower} of "
            f"{replicates} replicates, so its dominance no longer turns on cost alone and this "
            f"frequency must be read off the bootstrap, not enumerated")

    fast, subj = _mean_law(rates[EXACT_FASTER]), _mean_law(rates[EXACT_SUBJECT])
    p = sum(wf * ws for f, wf in fast.items() for t, ws in subj.items() if f > t)
    _, _, front = _suite_replicates(cells)
    return {"p": p, "pct": float(p) * 100.0,
            "outcomes": len(fast) * len(subj),
            "distinct_rates": {EXACT_FASTER: len(set(rates[EXACT_FASTER])),
                               EXACT_SUBJECT: len(set(rates[EXACT_SUBJECT]))},
            "condition_a": (dear_min, subj_max),
            "condition_b": (replicates - slower, replicates),
            "bootstrap_pct": 100.0 * front[EXACT_SUBJECT] / BOOT_B}


def _pct(xs, lo=2.5, hi=97.5):
    ys = sorted(xs)
    n = len(ys)
    at = lambda q: ys[min(n - 1, max(0, int(round(q / 100.0 * (n - 1)))))]   # noqa: E731
    return at(lo), at(hi)


def suite_uncertainty(cells: list) -> dict:
    """Per-cloud percentile intervals on E_X and G_X, frontier-membership frequency, and the pairwise
    difference verdicts Part 1's rule asks for (interval on the DIFFERENCE excluding zero).

    front_pct is the paired scheme (COST_PAIRED); front_pct_unpaired is the independent-draw scheme the
    frontier note reports as its sensitivity. Both are emitted so the note's comparison is generated
    rather than transcribed by hand.
    """
    ex, gx, front = _suite_replicates(cells, paired=True)
    _, _, front_u = _suite_replicates(cells, paired=False)
    out = {}
    for c in ex:
        out[c] = {"ex_ci": _pct(ex[c]), "gx_ci": _pct(gx[c]),
                  "front_pct": 100.0 * front[c] / BOOT_B,
                  "front_pct_unpaired": 100.0 * front_u[c] / BOOT_B}
    pairs = {}
    for a in ex:
        for b in ex:
            if a >= b:
                continue
            d = [x - y for x, y in zip(ex[a], ex[b])]
            lo, hi = _pct(d)
            pairs[(a, b)] = {"ci": (lo, hi), "decided": not (lo <= 0 <= hi)}
    out["_pairs"] = pairs
    return out


def frontier_scheme_sensitivity(cells: list, seeds) -> dict:
    """Paired minus unpaired frontier-membership frequency, repeated over several seeds.

    The two schemes share one stream at each seed, so at a given seed they run on identical time
    replicates and the difference isolates the cost draw. Repeating over seeds is what says whether that
    difference is a real effect or the frequency's own Monte Carlo noise at BOOT_B replicates; Part 5's
    frontier note reports it over the ten seeds listed in NOTE_SEEDS.
    """
    rows = {}
    for sd in seeds:
        _, _, fp = _suite_replicates(cells, paired=True, seed=sd)
        _, _, fu = _suite_replicates(cells, paired=False, seed=sd)
        for c in fp:
            rows.setdefault(c, []).append(100.0 * (fp[c] - fu[c]) / BOOT_B)
    return rows


NOTE_SEEDS = (20260907, 1, 2, 3, 7, 11, 101, 2026, 31337, 99991)


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
        # Report WHICH pair and WHICH task, not a per-cloud yes/no. Leave-one-out is a PAIRWISE
        # property, so collapsing it per cloud made one fragile pair read as two unstable clouds, and
        # read as a verdict withheld rather than a workload sensitivity disclosed (Part 4, S3).
        loo = "n/a (single pair)"
        others = [o for o in ANON if o != cl]
        if others:
            flips = []
            for o in others:
                r = weighting.leave_one_out(by_cloud[cl], by_cloud[o])
                for t in r["flips_on"]:
                    flips.append(f"{t} vs {ANON[o]}")
            loo = "stable" if not flips else "; ".join(flips)
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


# Author-only: --check diffs the generated rows against the paper source. Set ACSPEED_PAPER_TEX
# to combined_p5body.tex to use it; without it, --check skips the comparison.
TEX = os.environ.get("ACSPEED_PAPER_TEX", "")


def check_against_tex(cells: list) -> list:
    """Every generated row must appear verbatim in the paper. Catches the paper drifting from the
    results tree, in either direction."""
    if not TEX or not os.path.exists(TEX):
        print("SKIP: set ACSPEED_PAPER_TEX to combined_p5body.tex to diff the rows against the paper.")
        return None
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
        pairing = check_easy_pairing_alignment(cells)
        drift = check_against_tex(cells)          # None means the TeX diff was SKIPPED, not that it passed
        for b in problems:
            print("IDENTITY: " + b)
        for b in pairing:
            print("PAIRING: " + b)
        for d in (drift or []):
            print("DRIFT (row not in paper): " + d)
        if problems or pairing or drift:
            raise SystemExit(1)
        if drift is None:
            print("OK: identity holds and the Easy-cell pairing is run-for-run. TeX comparison "
                  "SKIPPED (ACSPEED_PAPER_TEX unset), so the rows were NOT diffed against the paper.")
        else:
            print("OK: identity holds, the Easy-cell pairing is run-for-run, and every generated row is "
                  "present verbatim in Part 5.")
        return
    problems = check_identity(cells) + check_easy_pairing_alignment(cells)
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
