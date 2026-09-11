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

What this reports, in the paper's two-layer split (Part 3, "The observed primary and the admitted
counterfactual references"): the OBSERVED PRIMARY is counterfactual-free -- each run's realized makespan
and its ratio to the observed best-achieved frontier (``best_ratio``), plus per-op execution excess against
its own observed floor. The ADMITTED COUNTERFACTUAL references are labelled as such and never asserted exact
-- the floor ``F_C`` (``floor_ratio``) and the selection excess (``selection_excess_s``, regret against a
schedule the agent did not run) are reported only on the designed suite and revised down when an observed
trace undercuts the floor. The two-sided bracket F_C <= optimum <= best-achieved is the honest headline (the
floor is loose, so the bracket WIDTH, not the floor ratio alone, is the first-class number).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from .reference import Edge, ReferenceGraph, decompose, gold_is_refuted, two_ratios

_START = "start"
_SERVED = "served"
_PROVISIONED = "provisioned"

# The authored gold is a DATED, VERSIONED artifact (literature: a refutable reference must be versioned
# so a floor revision does not silently change every previously reported ratio). The IPC precedent is
# NOT symmetric with ours and Part 5 is careful about this: the IPC's competitive score C*/C is taken
# against a PARTICIPANT-INDEPENDENT reference plan, and the best plan discovered is used as the
# reference only as a DOCUMENTED FALLBACK when no independent optimum is available (Taitler et al.,
# AI Magazine 45(2):280-296, 2024). Our floor is not the participant-independent case; it stands on
# the fallback's side of that distinction without sharing its mechanism, "that fallback being a
# documented last resort where ours is a standing rule that revises toward the best observed whenever
# a trace undercuts it" (Part 5, S3). Do not restate this as "IPC revises its reference plan the same
# way": that was the earlier wording here and it collapses the distinction the paper draws.
# Bump this when the floor-estimation rule or the graph structure
# changes; the per-run floor itself is data-derived (min-observed) and moves WITHIN a version, disclosed
# as ``floor_estimated_from_n`` and re-attributed by the refutation protocol.
# 1.1.0 (2026-09-04): the first-poll exclusion was refined from "any first-poll run" to "a first-poll run
# whose clock was stopped by a 4xx/5xx, or whose served URL does not carry its own run token". Under 1.0.0
# a cloud whose deploy call returns only once the service is live had EVERY run excluded from the floor and
# the frontier, so an entire architecture class contributed nothing to the Part 3 bracket at any n. Floors
# and ratios computed under 1.0.0 are not comparable to 1.1.0 ones.
GOLD_VERSION = "provision-deploy/1.1.0"

# Floor-sensitivity perturbation (literature P0: the competitive ratio is only as trustworthy as F_C, an
# ESTIMATED denominator; report how the ratio moves under +/- this fraction so "revised down if undercut"
# cannot read as "we tuned the denominator").
_FLOOR_EPS = 0.10


def _competitive_ratio_interval(m_actual: float, floor: float, best_achieved: float) -> List[float]:
    """The competitive ratio M/OPT reported as an INTERVAL, never a point (literature P0). Because
    F_C <= OPT <= best_achieved, the true ratio M/OPT lies in [M/best_achieved, M/F_C] = [best_ratio,
    floor_ratio]. Reporting a single number against a self-declared two-sided bracket is internally
    inconsistent; the interval is the honest object (the OR optimality-gap convention)."""
    return [round(m_actual / best_achieved, 3), round(m_actual / floor, 3)]


def _floor_sensitivity(best_achieved: float, floor: float, eps: float = _FLOOR_EPS) -> dict:
    """How the headline bracket ratio moves when the estimated floor F_C is perturbed +/- eps. A ratio
    that swings wildly under a small floor change is denominator-driven and must be read with that
    caveat; a stable one shows the estimate is not load-bearing. This is the auditable answer to the
    top reviewer objection to an estimated optimum."""
    return {
        "epsilon": eps,
        "F_C_minus_s": round(floor * (1 - eps), 1),
        "F_C_plus_s": round(floor * (1 + eps), 1),
        "bracket_ratio_at_F_C_minus": round(best_achieved / (floor * (1 - eps)), 3) if floor > 0 else None,
        "bracket_ratio_at_F_C_plus": round(best_achieved / (floor * (1 + eps)), 3) if floor > 0 else None,
        "note": ("bracket ratio = best-achieved / F_C, recomputed at F_C(1-eps) and F_C(1+eps); the spread "
                 "is the estimated floor's leverage on the headline (a stable spread means the ratio is "
                 "not denominator-driven)."),
    }


def _reached_goal(rec: dict) -> bool:
    """The operation reached the goal iff the app served (a t1 was recorded, so time_to_serving_s is a
    positive number). This matters: a NEVER-SERVED run still carries a critical-path makespan from its
    failed attempt, and the paper (Part 3 Sec. 2) defines both the floor and the best-achieved frontier
    over 'valid traces that reached the goal', so a non-serving run must be excluded from both."""
    tts = rec.get("time_to_serving_s")
    return isinstance(tts, (int, float)) and tts > 0


def _is_suspect(rec: dict) -> bool:
    """Is this run's t1 unusable as a 'valid trace that reached the goal'?

    Serving on the very FIRST poll is the trigger, but it is not by itself the defect. Two structurally
    different things produce it, and only one is a measurement error:

    (a) A platform EDGE answered before the app did (a container-service hostname 404ing the moment DNS
        exists, an IAM 403 on an unwired route). t1 then measures 'when the name appeared', is
        false-early, and its clipped split would drag the global-min floor down and inflate every floor
        ratio. EXCLUDE.
    (b) The deploy call does not hand back the URL until the service is ALREADY serving, so the poller
        cannot observe a not-yet-serving state however early it starts. t1 is then an honest UPPER bound
        on time-to-serving, and the run is a perfectly valid trace. INCLUDE: excluding it drops an
        entire architecture class (every serverless-container run) from the Part 3 floor and frontier
        rather than measuring it, which is a far larger error than the loose bound it avoids.

    The discriminator is the status that STOPPED THE CLOCK: a 4xx is (a) -- the same signature the live
    edge-origin check in ``autorun.is_serving_ex`` now refuses -- and a 2xx/3xx is (b).

    A LEFTOVER deployment from an earlier run would also answer on the first poll, and it is a real
    hazard. It is ruled out separately: resources are named per run, so the run's own token appearing in
    the served URL means the URL cannot belong to an earlier run. When the token is absent from the URL
    (a custom domain, a proxy) the leftover cannot be ruled out, so the run stays suspect."""
    serving = rec.get("serving") or {}
    if not serving.get("served_on_first_poll"):
        return False
    code = serving.get("http_code")
    try:
        code_i = int(code)
    except (TypeError, ValueError):
        return True  # cannot tell what stopped the clock -> stay conservative
    if 400 <= code_i < 600:
        return True  # (a) an edge, or an error, answered before the app
    token = rec.get("run_token")
    url = rec.get("url") or ""
    if not token or token not in url:
        return True  # cannot rule out a leftover deployment
    return False  # (b) structurally first-poll: a valid trace with an upper-bound t1


def _provision_weights(records: Sequence[dict]):
    """Pull (floor, [(run, makespan, platform), ...], n_excluded_suspect) from acspeed run records,
    over CLEAN goal-reaching runs only. floor = min-observed critical-platform-time; a run's realized
    weight = its critical-path makespan (split.makespan_s, falling back to time_to_serving_s). Runs
    that never served, are flagged suspect (see ``_is_suspect``), or lack a usable split are skipped."""
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
            "competitive_ratio_interval": _competitive_ratio_interval(makespan, fc, best_achieved),
            "execution_excess_s": round(dec.execution_excess, 1),
            "selection_excess_s": round(dec.selection_excess, 1),
            "identity_holds": dec.identity_holds,
        })

    refuted_to = gold_is_refuted(fc, [m for _run, m, _p in runs])  # a valid trace below the floor -> revise
    return {
        "instance": "provision (1-op degenerate)",
        "gold_version": GOLD_VERSION,
        "F_C_s": round(fc, 1),
        "floor_estimated_from_n": len(runs),
        "best_achieved_s": round(best_achieved, 1),
        "bracket_low_s": round(fc, 1),
        "bracket_high_s": round(best_achieved, 1),
        "bracket_width_s": round(best_achieved - fc, 1),
        "bracket_ratio": round(best_achieved / fc, 3) if fc > 0 else None,
        "floor_sensitivity": _floor_sensitivity(best_achieved, fc),
        "refutation": {
            "protocol": ("F_C is the min-observed critical-platform-time; if any valid goal-reaching trace "
                         "undercuts it, the gold is refuted and revised DOWN to that trace, bumping the "
                         "floor within this gold_version. Every reported ratio is bound to (gold_version, "
                         "floor_estimated_from_n)."),
            "refuted": refuted_to is not None,
            "revise_floor_to_s": round(refuted_to, 1) if refuted_to is not None else None,
        },
        "n_runs": len(runs),
        "n_excluded_suspect": excluded_suspect,
        "per_run": per_run,
        "note": ("F_C = min-observed critical-platform-time over clean goal-reaching runs (empirical "
                 "floor, loose by construction, so the bracket width is the first-class result). "
                 "First-poll-flagged (possible leftover-deployment) runs are excluded from the floor "
                 "and frontier. Selection excess is 0 for this on-suite degenerate single-operation "
                 "task (one edge, no alternatives); the multi-VM suites add it. Two-layer disclosure "
                 "(Part 3): best_ratio and execution excess are the OBSERVED primary (counterfactual-"
                 "free); floor_ratio and selection excess are the ADMITTED counterfactual (vs F_C, a "
                 "schedule the agent did not run; bounded and refutable, on-suite only)."),
        "layers": {
            "observed_primary": ["makespan_s", "best_ratio", "execution_excess_s"],
            "admitted_counterfactual": ["F_C_s", "floor_ratio", "selection_excess_s"],
        },
    }


# ---------------------------------------------------------------------------
# The richer, multi-operation deploy gold (where selection excess becomes non-trivial).
#
# The degenerate ``part3_provision`` above scores "deploy X" as ONE provision edge, so selection excess
# is 0 by construction (one edge, no alternatives) -- honest for a platform that BUNDLES provisioning
# into a single call the agent cannot re-order. When the agent DOES issue separable operations (a
# second app in the Medium tier; a managed datastore provisioned distinctly from its app compute; a
# cross-architecture run that chose App-Runner-vs-VM-vs-container), the session is a genuine
# operation-DAG with ALTERNATIVE schedules, and choosing a worse one (serializing what could overlap,
# a slower datastore topology, an extra redeploy) is REAL selection excess. This block instantiates the
# SAME reference-math (``reference.py``) on that richer graph, per Part 3 Sec 1.2:
#
#   "An edge is one scheduling step: launching one operation, or a CONCURRENT BUNDLE whose floor weight
#    is the bundle's critical path."
#
# So the two-resource provision has two alternatives as two edges from start -> provisioned:
#   * CONCURRENT bundle  floor = max(floor_app, floor_db)            (the reference-optimal path)
#   * SERIAL             floor = floor_app + floor_db                (selection excess = min(app, db))
# and a co-located-container topology is a third alternative with its own floor. F_C = shortest path
# picks the min-makespan alternative; a run that took a heavier one pays selection excess exactly equal
# to the extra floor its choice carried, with execution excess the residual above its chosen floor.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProvisionAlternative:
    """One way to reach the ``provisioned`` state, as a SINGLE scheduling-epoch edge whose floor weight
    is that alternative's critical path (Part 3 Sec 1.2). Examples for a two-resource app (app compute +
    datastore): a CONCURRENT bundle weighs ``max(floor_app, floor_db)``; a SERIAL choice weighs
    ``floor_app + floor_db``; a co-located datastore CONTAINER folds the store into the app host and
    weighs the single-host provision floor. ``label`` is how a run's trajectory names the branch it took."""

    label: str
    floor: float


def build_deploy_graph(provision_alternatives: Sequence[ProvisionAlternative],
                       deploy_floor: float) -> ReferenceGraph:
    """A two-stage provision -> deploy operation-state graph:
    ``start --(alternative_i)--> provisioned --deploy--> served``. Each provision alternative is one
    edge (one scheduling epoch); ``F_C = floor_makespan()`` picks the minimum-makespan alternative + the
    deploy. The graph IS the goal (Part 3: efficiency is set-relative), so a run is scored only against
    the alternatives that actually reach THIS goal. Raises if no alternatives are given (an empty graph
    has no floor)."""
    alts = list(provision_alternatives)
    if not alts:
        raise ValueError("a deploy graph needs at least one provision alternative")
    edges = [Edge(_START, _PROVISIONED, a.label, floor=a.floor) for a in alts]
    edges.append(Edge(_PROVISIONED, _SERVED, "deploy", floor=deploy_floor))
    return ReferenceGraph(edges, _START, _SERVED)


def score_deploy_run(graph: ReferenceGraph, chosen_provision_label: str,
                     provision_actual: float, deploy_actual: float):
    """Decompose one run whose trajectory took ``chosen_provision_label`` then the deploy edge, with the
    measured actual times. Returns the Part-3 :class:`reference.Decomposition` (selection + execution
    excess, telescoping advantages, and the numeric identity self-check). The floor twin runs the chosen
    edges at floor speed, so selection excess is exactly the extra floor the chosen alternative carried
    over F_C, and execution excess is the residual above the chosen floor."""
    prov = next((e for e in graph.edges if e.src == _START and e.label == chosen_provision_label), None)
    if prov is None:
        raise ValueError(f"no provision alternative labelled {chosen_provision_label!r} in the graph")
    dep = next((e for e in graph.edges if e.dst == _SERVED), None)
    if dep is None:
        raise ValueError("graph has no deploy edge into the served goal")
    trajectory = [
        Edge(_START, _PROVISIONED, prov.label, floor=prov.floor, actual=provision_actual),
        Edge(_PROVISIONED, _SERVED, "deploy", floor=dep.floor, actual=deploy_actual),
    ]
    return decompose(graph, trajectory)


# a record -> (chosen_provision_label, provision_actual_s, deploy_actual_s, makespan_s) or None to skip
DeployExtract = Callable[[dict], Optional[tuple]]


def part3_deploy(records: Sequence[dict], graph: ReferenceGraph,
                 extract: DeployExtract, *, instance: str = "provision-deploy (multi-op)") -> Optional[dict]:
    """Author + evaluate the multi-operation deploy gold from run history, given a PRE-BUILT reference
    graph (floors already decided: authored for the published gold, or min-observed per operation) and an
    ``extract`` that maps each record to its chosen branch + measured times. Same two-layer disclosure,
    bracket, floor-sensitivity and refutation shape as :func:`part3_provision`, but with a genuine
    selection/execution split because the graph carries alternatives. Returns None when no record yields
    a usable trajectory (defensive, exactly like the degenerate gold)."""
    fc = graph.floor_makespan()
    scored = []                     # (run, label, makespan, Decomposition) for cleanly-scored runs
    refuted_runs = []               # runs whose measured leg fell below an authored floor (the refutation)
    for r in records:
        got = extract(r)
        if not got:
            continue
        label, prov_actual, dep_actual, makespan = got
        if not (isinstance(makespan, (int, float)) and makespan > 0):
            continue
        try:
            dec = score_deploy_run(graph, label, float(prov_actual), float(dep_actual))
        except ValueError:
            # a measured provision/deploy leg is below its authored floor: the authored floor is REFUTED
            # for that branch (Part 3 Sec 2.2 layer 2). Record it as a refutation, never crash on it.
            refuted_runs.append({"run": r.get("run"), "makespan_s": round(float(makespan), 1)})
            continue
        scored.append((r.get("run"), label, float(makespan), dec))
    if not scored:
        return None

    best_achieved = min(m for _run, _l, m, _d in scored)
    per_run = []
    for run, label, makespan, dec in scored:
        ratios = two_ratios(makespan, fc, best_achieved)
        per_run.append({
            "run": run,
            "chosen_provision": label,
            "makespan_s": round(makespan, 1),
            "floor_ratio": ratios["floor_ratio"],
            "best_ratio": ratios["best_ratio"],
            "competitive_ratio_interval": _competitive_ratio_interval(makespan, fc, best_achieved),
            "execution_excess_s": round(dec.execution_excess, 1),
            "selection_excess_s": round(dec.selection_excess, 1),
            "identity_holds": dec.identity_holds,
        })

    return {
        "instance": instance,
        "gold_version": GOLD_VERSION,
        "F_C_s": round(fc, 1),
        "best_achieved_s": round(best_achieved, 1),
        "bracket_low_s": round(fc, 1),
        "bracket_high_s": round(best_achieved, 1),
        "bracket_width_s": round(best_achieved - fc, 1),
        "bracket_ratio": round(best_achieved / fc, 3) if fc > 0 else None,
        "floor_sensitivity": _floor_sensitivity(best_achieved, fc),
        "refutation": {
            "protocol": ("A run whose measured provision/deploy leg fell BELOW its authored floor refutes "
                         "that branch's floor; revise it down to the observed leg, bump the floor within "
                         "this gold_version, and re-attribute. Ratios are bound to (gold_version, F_C)."),
            "refuted": len(refuted_runs) > 0,
            "refuted_runs": refuted_runs,
            "revise_floor_to_s": (min(rr["makespan_s"] for rr in refuted_runs) if refuted_runs else None),
        },
        "n_runs": len(scored),
        "alternatives": [{"label": e.label, "floor_s": round(e.floor, 1)}
                         for e in graph.edges if e.src == _START],
        "per_run": per_run,
        "note": ("Multi-operation deploy gold: F_C is the shortest floor path over the provision "
                 "alternatives + deploy. Selection excess is the extra floor a run's chosen schedule "
                 "carried over the min-makespan alternative (serializing the parallelizable, a slower "
                 "datastore topology, or an extra operation); execution excess is the residual above the "
                 "chosen floor. Two-layer disclosure as in the degenerate gold."),
        "layers": {
            "observed_primary": ["makespan_s", "best_ratio", "execution_excess_s"],
            "admitted_counterfactual": ["F_C_s", "floor_ratio", "selection_excess_s"],
        },
    }
