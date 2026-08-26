"""Part 4: weighting and aggregation into one headline, without a chosen weight.

Aggregation is summation of critical-path wall-clock seconds: summing homogeneous seconds adds no chosen
coefficient (each second counts once), so Part 4 adds no new chosen variable beyond the wall-clock currency
Part 1 already fixed. The critical path weights operations, the suite total weights tasks.

The headline is the total per-task wall-clock over a matched, capability-normalized, disclosed suite
(:func:`suite_total`, the symbol ``E_X``), carried with a reference-invariant geometric-mean-of-ratios
companion (:func:`geomean_ratio`, the symbol ``G_X``); a cross-cloud claim stands only when both agree
(:func:`suite_verdict`), defended by a drop-any-task sensitivity check (:func:`leave_one_out`). Each per-task
comparison is a matched-block comparison (task = block, cloud = treatment, wall-clock = response; Fisher's
blocking) decided by non-overlapping confidence intervals (:func:`more_efficient`, reusing Part 1's
reproducibility), never by a single pair of runs.

The one direction wall-clock is gameable -- buying speed with a bigger, costlier machine -- is closed not by
a chosen time-versus-cost exchange rate but by a two-input (wall-clock, resource-cost) cost-performance
frontier under the Pareto-Koopmans criterion (:func:`dominates`, :func:`pareto_frontier`,
:func:`classify_run`): a run is efficient only if no other run uses no more time AND no more cost; a run that
spends more cost for no less time carries a positive cost slack and is dominated (inefficient), so speed
cannot be bought. Under-provisioning is penalized on time (a larger makespan, Part 3 selection excess);
over-provisioning on cost. Infrastructure resource-cost is resource-seconds priced by a dated rate table
(the primary frontier input); agent token-cost is a legitimate secondary input the same frontier can carry.

New symbols: the suite total ``E_X`` and companion ``G_X``, both summaries of quantities defined in
Parts 1-3; no new free parameter. Capability normalization of a task's makespan across clouds is Part 1's
job (``capability.normalize_phase``); this module aggregates makespans taken as already comparable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence

from .repro import bootstrap_ci, different, geomean

_EPS = 1e-9


# --------------------------------------------------------------------------------------------------
# Per-task efficiency: the matched-block comparison (Part 4, Section 2)
# --------------------------------------------------------------------------------------------------

def more_efficient(a_samples: Sequence[float], b_samples: Sequence[float],
                   confidence: float = 0.95) -> str:
    """Matched-block per-task verdict: on one fixed task, is cloud ``"A"`` or ``"B"`` more efficient?

    Lower wall-clock wins, but only when the two clouds' makespan distributions are distinguishable:
    the verdict is decided by non-overlapping confidence intervals over repeated runs (Part 1's
    reproducibility protocol), never by a single pair of runs. Returns ``"A"``, ``"B"``, or
    ``"indistinguishable"``.
    """
    if not a_samples or not b_samples:
        raise ValueError("both clouds need at least one makespan sample")
    ea = bootstrap_ci(a_samples, confidence)
    eb = bootstrap_ci(b_samples, confidence)
    if not different(ea, eb):
        return "indistinguishable"
    return "A" if ea.value < eb.value else "B"


# --------------------------------------------------------------------------------------------------
# Suite aggregation: total time (headline) + geometric-mean companion (Part 4, Section 3)
# --------------------------------------------------------------------------------------------------

def _as_task_map(makespans) -> Dict[str, float]:
    m = dict(makespans) if isinstance(makespans, Mapping) else {str(i): v for i, v in enumerate(makespans)}
    if not m:
        raise ValueError("empty suite")
    for t, v in m.items():
        if v < 0:
            raise ValueError(f"makespan for task {t!r} is negative")
    return m


def suite_total(makespans) -> float:
    """Headline ``E_X``: total per-task wall-clock summed over the fixed suite. Each task is measured in
    isolation, so the sum is the suite's completion time under serial, isolated execution (cross-task
    parallelism and shared setup are out of scope by design). Lower is more efficient. ``makespans`` is a
    task -> makespan mapping or a sequence of makespans."""
    return sum(_as_task_map(makespans).values())


def _aligned(makespans, ref) -> List[str]:
    m, r = _as_task_map(makespans), _as_task_map(ref)
    if set(m) != set(r):
        raise ValueError("makespans and reference must cover the same tasks")
    return sorted(m)


def geomean_ratio(makespans, ref) -> float:
    """Companion ``G_X``: geometric mean of the per-task ratios ``M_X(T)/M_ref(T)``, each task counting
    once and dimensionless, whose cross-cloud ranking is invariant to the choice of ``ref``, the only mean
    with that property (Fleming and Wallace 1986). ``ref`` maps each task to the makespan of a fixed,
    disclosed baseline cloud used purely as a per-task normalizer (NOT Part 3's reference-optimal makespan;
    the ranking of ``G_X`` across clouds does not depend on which baseline is chosen)."""
    m, r = _as_task_map(makespans), _as_task_map(ref)
    tasks = _aligned(makespans, ref)
    for t in tasks:
        if r[t] <= 0:
            raise ValueError(f"reference makespan for task {t!r} must be positive")
    return geomean([m[t] / r[t] for t in tasks])


def _cmp(a: float, b: float) -> str:
    if abs(a - b) <= _EPS:
        return "tie"
    return "A" if a < b else "B"


def suite_verdict(a_makespans, b_makespans, ref) -> dict:
    """Compare two clouds over the suite by BOTH the total-time headline and the geometric-mean companion.
    A ranking is returned only when the two summaries AGREE; otherwise the split is reported rather than
    collapsed to one number by fiat (Part 4, Section 3). ``ref`` is the per-task reference for the ratios."""
    a_total, b_total = suite_total(a_makespans), suite_total(b_makespans)
    a_gm, b_gm = geomean_ratio(a_makespans, ref), geomean_ratio(b_makespans, ref)
    total_winner, gm_winner = _cmp(a_total, b_total), _cmp(a_gm, b_gm)
    agree = total_winner == gm_winner and total_winner in ("A", "B")
    return {
        "a_total": a_total, "b_total": b_total,
        "a_geomean_ratio": a_gm, "b_geomean_ratio": b_gm,
        "total_winner": total_winner, "geomean_winner": gm_winner,
        "agree": agree,
        "verdict": total_winner if agree else "unresolved",
    }


def leave_one_out(a_makespans, b_makespans) -> dict:
    """Drop-any-task sensitivity (Part 4, Section 3): the cross-cloud total-time ranking must survive
    removing any single task. Returns whether the ranking is robust, which tasks flip it, and the range of
    the headline difference ``E_A - E_B`` under leave-one-out. A ranking that flips on one task is
    unresolved, not published."""
    a, b = _as_task_map(a_makespans), _as_task_map(b_makespans)
    if set(a) != set(b):
        raise ValueError("both clouds must cover the same tasks")
    tasks = sorted(a)
    if len(tasks) < 2:
        raise ValueError("need at least two tasks for a leave-one-out check")
    full = _cmp(suite_total(a), suite_total(b))
    flips: List[str] = []
    diffs: List[float] = []
    for drop in tasks:
        aa = {t: v for t, v in a.items() if t != drop}
        bb = {t: v for t, v in b.items() if t != drop}
        diffs.append(suite_total(aa) - suite_total(bb))
        if _cmp(suite_total(aa), suite_total(bb)) != full:
            flips.append(drop)
    return {
        "full_verdict": full,
        "robust": not flips and full in ("A", "B"),
        "flips_on": flips,
        "diff_range": (min(diffs), max(diffs)),
    }


# --------------------------------------------------------------------------------------------------
# The cost-performance frontier: Pareto-Koopmans dominance (Part 4, Section 4)
# --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Run:
    """One operating point on the (wall-clock, resource-cost) plane: ``time`` is the makespan and ``cost``
    is the infrastructure resource-cost (resource-seconds priced by a dated rate table). ``label`` names
    the configuration (e.g. the chosen instance class)."""

    label: str
    time: float
    cost: float

    def __post_init__(self) -> None:
        if self.time < 0 or self.cost < 0:
            raise ValueError(f"run {self.label!r} has a negative input (time={self.time}, cost={self.cost})")


def dominates(a: Run, b: Run) -> bool:
    """Pareto-Koopmans dominance: ``a`` dominates ``b`` iff ``a`` uses no more of either input and strictly
    less of at least one (``a.time <= b.time`` and ``a.cost <= b.cost``, not equal on both). A run that
    spends more cost for no less time is dominated (a positive cost slack), so speed cannot be bought."""
    no_worse = a.time <= b.time + _EPS and a.cost <= b.cost + _EPS
    strictly_better = a.time + _EPS < b.time or a.cost + _EPS < b.cost
    return no_worse and strictly_better


def is_tradeoff(a: Run, b: Run) -> bool:
    """True iff ``a`` and ``b`` are mutually non-dominated (one faster, the other cheaper): a genuine
    time-cost tradeoff, i.e. 'buying speed' -- moving along the frontier, not an efficiency win either way."""
    return not dominates(a, b) and not dominates(b, a)


def pareto_frontier(runs: Sequence[Run]) -> List[Run]:
    """The efficient (non-dominated) runs: the cost-performance frontier. Efficiency is the Pareto-Koopmans
    criterion (no input reducible without raising another), so genuine time-cost tradeoffs stay on the
    frontier and wasteful (same-time-more-cost) or slower runs are excluded. Order is preserved."""
    runs = list(runs)
    return [r for r in runs if not any(dominates(o, r) for o in runs if o is not r)]


def classify_run(run: Run, runs: Sequence[Run]) -> dict:
    """Classify one run against the observed pool (Part 4, Section 4). ``efficient`` is True iff it is on
    the frontier; otherwise it is dominated, and ``reason`` distinguishes ``"cost_slack"`` (a run at no more
    time exists at strictly lower cost -- it bought no speed) from ``"slower"`` (a strictly faster run at no
    more cost exists). ``dominated_by`` lists the dominating configurations."""
    dommers = [o for o in runs if o is not run and dominates(o, run)]
    if not dommers:
        return {"efficient": True, "dominated_by": [], "reason": None}
    same_time = any(abs(o.time - run.time) <= _EPS for o in dommers)
    return {
        "efficient": False,
        "dominated_by": [o.label for o in dommers],
        "reason": "cost_slack" if same_time else "slower",
    }
