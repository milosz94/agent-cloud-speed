"""Part 3 reference-optimal gold, instantiated from run history (the 'author the gold' step).

`reference.py` is the pure Part-3 math (operation-state graph, floor cost-to-go, telescoping
decomposition, two-ratio bracket). This module is the glue that AUTHORS a concrete gold instance from
acspeed run records, so Part 3 stops being a library and produces a real number.

The degenerate instance is the PROVISION operation as a ONE-edge operation-state graph
(start -> served). Its floor weight is the min-observed critical-platform-time (the empirical lower
bound, paper Part 3 Sec. 2 'the floor weights are estimated as minimum-observed
critical-platform-times'); the realized weight of a run is its critical-path makespan. For a
single-operation suite there are no alternative edges, so selection excess is 0 by construction and
all excess over the floor is execution excess -- honest for a linear op sequence. The multi-VM suites
add alternative edges (flavor / order / overlap choices) where selection excess becomes non-trivial;
they instantiate the SAME builder with a richer graph, not a different one.

What this reports (paper Part 3 headline): the two-sided bracket F_C <= optimum <= best-achieved as a
first-class result (the floor is loose, so the bracket width, not the floor ratio alone, is the honest
number), plus each run's floor ratio and its exact selection/execution split.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .reference import Edge, ReferenceGraph, decompose, two_ratios

_START = "start"
_SERVED = "served"


def _reached_goal(rec: dict) -> bool:
    """The operation reached the goal iff the app served (a t1 was recorded, so time_to_serving_s is a
    positive number). This matters: a NEVER-SERVED run still carries a critical-path makespan from its
    failed attempt, and the paper (Part 3 Sec. 2) defines both the floor and the best-achieved frontier
    over 'valid traces that reached the goal', so a non-serving run must be excluded from both."""
    tts = rec.get("time_to_serving_s")
    return isinstance(tts, (int, float)) and tts > 0


def _is_suspect(rec: dict) -> bool:
    """A run flagged served_on_first_poll is suspect: the URL answered on the very first poll, which
    may be a LEFTOVER deployment or a false-early t1 (the acknowledged concurrent-poller instrument
    defect, same class as the isso re-run). Its clipped split gives an artificially low
    critical-platform that would drag the global-min floor down and inflate every floor ratio, so it is
    not a clean 'valid trace that reached the goal' and is excluded from the floor and the frontier."""
    return bool((rec.get("serving") or {}).get("served_on_first_poll"))


def _provision_weights(records: Sequence[dict]):
    """Pull (floor, [(run, makespan, platform), ...], n_excluded_suspect) from acspeed run records,
    over CLEAN goal-reaching runs only. floor = min-observed critical-platform-time; a run's realized
    weight = its critical-path makespan (split.makespan_s, falling back to time_to_serving_s). Runs
    that never served, are flagged suspect (first-poll), or lack a usable split are skipped."""
    platforms: List[float] = []
    runs = []
    excluded = 0
    for r in records:
        if not _reached_goal(r):
            continue
        if _is_suspect(r):
            excluded += 1
            continue
        sp = r.get("split") or {}
        platform = sp.get("critical_platform_s")
        makespan = sp.get("makespan_s")
        if not isinstance(makespan, (int, float)):
            makespan = r.get("time_to_serving_s")
        if isinstance(platform, (int, float)) and isinstance(makespan, (int, float)) and makespan > 0:
            platforms.append(float(platform))
            runs.append((r.get("run"), float(makespan), float(platform)))
    return (min(platforms) if platforms else None), runs, excluded


def part3_provision(records: Sequence[dict]) -> Optional[dict]:
    """Author + evaluate the degenerate provision gold from run history. Returns None when no run
    carries a usable acspeed split; otherwise F_C, the two-ratio bracket (with width + ratio as
    first-class results), and each run's floor ratio + exact selection/execution split."""
    floor, runs, excluded_suspect = _provision_weights(records)
    if floor is None or not runs:
        return None

    graph = ReferenceGraph([Edge(_START, _SERVED, "provision", floor=floor)], _START, _SERVED)
    fc = graph.floor_makespan()                       # == floor for the one-edge graph
    best_achieved = min(m for _run, m, _p in runs)    # DEA frontier: min-observed valid makespan

    per_run = []
    for run, makespan, _platform in runs:
        trajectory = [Edge(_START, _SERVED, "provision", floor=floor, actual=makespan)]
        dec = decompose(graph, trajectory)
        ratios = two_ratios(makespan, fc, best_achieved)
        per_run.append({
            "run": run,
            "makespan_s": round(makespan, 1),
            "floor_ratio": ratios["floor_ratio"],
            "best_ratio": ratios["best_ratio"],
            "execution_excess_s": round(dec.execution_excess, 1),
            "selection_excess_s": round(dec.selection_excess, 1),
            "identity_holds": dec.identity_holds,
        })

    return {
        "instance": "provision (1-op degenerate)",
        "F_C_s": round(fc, 1),
        "best_achieved_s": round(best_achieved, 1),
        "bracket_low_s": round(fc, 1),
        "bracket_high_s": round(best_achieved, 1),
        "bracket_width_s": round(best_achieved - fc, 1),
        "bracket_ratio": round(best_achieved / fc, 3) if fc > 0 else None,
        "n_runs": len(runs),
        "n_excluded_suspect": excluded_suspect,
        "per_run": per_run,
        "note": ("F_C = min-observed critical-platform-time over clean goal-reaching runs (empirical "
                 "floor, loose by construction, so the bracket width is the first-class result). "
                 "First-poll-flagged (possible leftover-deployment) runs are excluded from the floor "
                 "and frontier. Selection excess is 0 for this on-suite degenerate single-operation "
                 "task (one edge, no alternatives); the multi-VM suites add it."),
    }
