"""Part 3: the reference-optimal trace and the exact decomposition of excess over the floor.

A session's efficiency is a COMPETITIVE RATIO to a reference-optimal makespan, bracketed between a
provable-given-its-weights floor ``F_C`` and an achievable best-observed frontier. The EXACT additive,
per-decision decomposition is of the excess OVER THE FLOOR (not over the true optimum, whose own
above-floor execution cannot be charged to the agent), computed with the floor cost-to-go ``V_F`` of an
operation-state graph::

    excess_over_floor = M_actual - F_C = sum_i advantage(s_i, a_i)     (telescoping identity, Sec. 3)
                      = execution_excess + selection_excess            (floor-twin split, Sec. 4)

where ``V_F(s)`` is the minimum-makespan-to-goal at floor weights (a shortest cost-to-go on the graph),
``advantage(s_i, a_i) = c_i + V_F(s_{i+1}) - V_F(s_i) >= 0`` (Bellman optimality of ``V_F``), the floor
twin runs the agent's chosen edges at floor speed (makespan ``M_twin``), so
``execution_excess = M_actual - M_twin`` and ``selection_excess = M_twin - F_C``.

Modeling note (Part 3, Section 1.2). An edge is one scheduling step: launching one operation, or a
concurrent bundle whose floor weight is the bundle's critical path. The realized makespan is then the sum
of the taken edges' weights along the decision-epoch sequence (``sum_i c_i = M_actual`` by construction),
and the reference-optimal is a shortest cost path in the graph. Under unlimited concurrency the
min-makespan schedule reduces to this shortest path; edge weights come from Part 1's critical-path
instrument, and an edge is a Part 2 operation (or bundle of them).

Non-negativity in this shortest-path model is immediate: ``V_F(src) <= floor(e) + V_F(dst)`` (the shortest
cost-to-go relaxes any single edge), so ``advantage(e) = actual(e) + V_F(dst) - V_F(src) >= actual(e) -
floor(e) >= 0``. Under genuine bounded-concurrency scheduling (not implemented here) the same
non-negativity holds by a domination-and-monotonicity argument on the floor cost-to-go (Part 3, Section 3).
The two-input (wall-clock, resource-cost) Pareto-Koopmans frontier that scores capability selection so
speed cannot be bought is Part 4 (see ``weighting``).

This module intentionally works on explicit floor/actual weights rather than reaching into
``operation.py`` so the reference math is testable in isolation; an ``Operation``'s ``wall_clock()`` is the
natural ``actual`` for an edge, and its per-op floor (min-observed critical-platform-time) the ``floor``.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

_EPS = 1e-9


@dataclass(frozen=True)
class Edge:
    """One operation as a graph edge: a transition ``src -> dst`` with a ``floor`` weight (minimum
    achievable critical-path time) and, when realized, an ``actual`` weight (measured critical-path
    time). ``actual`` is never below ``floor`` (the floor is a lower bound on achievable time)."""

    src: str
    dst: str
    label: str
    floor: float
    actual: Optional[float] = None

    def __post_init__(self) -> None:
        if self.floor < 0:
            raise ValueError(f"floor weight must be non-negative (edge {self.label!r})")
        if self.actual is not None and self.actual + _EPS < self.floor:
            raise ValueError(
                f"actual {self.actual} is below floor {self.floor} for edge {self.label!r}: "
                "a floor weight was over-estimated, revise it downward"
            )


class ReferenceGraph:
    """An operation-state graph. Nodes are task-relevant states; edges are operations weighted by their
    floor. The floor cost-to-go ``V_F(s)`` is the minimum total floor weight of a path from ``s`` to the
    goal; the reference-optimal floor makespan is ``F_C = V_F(start)``. Weights are non-negative, so the
    cost-to-go is a Dijkstra shortest path on the reversed graph."""

    def __init__(self, edges: Sequence[Edge], start: str, goal: str) -> None:
        self.edges: List[Edge] = list(edges)
        self.start = start
        self.goal = goal
        self.nodes = {start, goal}
        self._incoming: Dict[str, List[Edge]] = {}
        for e in self.edges:
            self.nodes.add(e.src)
            self.nodes.add(e.dst)
            self._incoming.setdefault(e.dst, []).append(e)

    def cost_to_go(self) -> Dict[str, float]:
        """``V_F``: shortest floor-weighted distance from each node to the goal. Unreachable nodes map to
        ``inf``."""
        dist: Dict[str, float] = {n: math.inf for n in self.nodes}
        dist[self.goal] = 0.0
        pq: List[Tuple[float, str]] = [(0.0, self.goal)]
        while pq:
            d, node = heapq.heappop(pq)
            if d > dist[node] + _EPS:
                continue
            for e in self._incoming.get(node, []):
                nd = d + e.floor
                if nd + _EPS < dist[e.src]:
                    dist[e.src] = nd
                    heapq.heappush(pq, (nd, e.src))
        return dist

    def floor_makespan(self) -> float:
        """``F_C = V_F(start)``: the reference-optimal makespan at floor weights."""
        fc = self.cost_to_go()[self.start]
        if math.isinf(fc):
            raise ValueError("goal is not reachable from start under the floor weights")
        return fc


@dataclass
class Decomposition:
    """The exact decomposition of a realized trajectory's excess over the floor (Part 3, Sections 3-4).
    ``advantages`` are the per-decision terms that telescope to ``excess_over_floor``; the floor-twin
    split gives the same total as ``execution_excess + selection_excess``. ``identity_holds`` is a
    numeric self-check that both reconstructions equal the excess."""

    m_actual: float
    floor: float  # F_C
    excess_over_floor: float
    execution_excess: float
    selection_excess: float
    advantages: List[float]
    identity_holds: bool


def decompose(graph: ReferenceGraph, trajectory: Sequence[Edge]) -> Decomposition:
    """Decompose a realized trajectory's excess over the floor.

    ``trajectory`` is the ordered sequence of edges the agent actually took (each with ``actual`` set),
    a contiguous walk from ``graph.start`` to ``graph.goal``. Returns the telescoping per-decision
    advantages and the floor-twin selection/execution split. A non-completing run (not ending at the
    goal) raises: score it against the residual cost-to-go or use :func:`execution_excess_only`.
    """
    if not trajectory:
        raise ValueError("empty trajectory")
    if trajectory[0].src != graph.start:
        raise ValueError("trajectory does not begin at the graph start state")
    if trajectory[-1].dst != graph.goal:
        raise ValueError(
            "trajectory does not end at the goal state (non-completing run); score against the residual "
            "cost-to-go V_F(s_N) or restrict to execution_excess_only()"
        )
    for a, b in zip(trajectory, trajectory[1:]):
        if a.dst != b.src:
            raise ValueError("trajectory edges are not contiguous")
    for e in trajectory:
        if e.actual is None:
            raise ValueError(f"edge {e.label!r} has no actual time")

    vf = graph.cost_to_go()
    fc = vf[graph.start]
    if math.isinf(fc):
        raise ValueError("goal unreachable at floor weights")

    m_actual = sum(e.actual for e in trajectory)
    m_twin = sum(e.floor for e in trajectory)  # the chosen edges, run at floor speed
    execution_excess = m_actual - m_twin
    selection_excess = m_twin - fc
    excess = m_actual - fc

    advantages: List[float] = []
    for e in trajectory:
        if math.isinf(vf[e.src]) or math.isinf(vf[e.dst]):
            raise ValueError(f"a state on the trajectory has no path to goal at floor weights (edge {e.label!r})")
        advantages.append(e.actual + vf[e.dst] - vf[e.src])

    identity_holds = (
        abs(sum(advantages) - excess) < 1e-6
        and abs((execution_excess + selection_excess) - excess) < 1e-6
    )
    return Decomposition(
        m_actual=m_actual,
        floor=fc,
        excess_over_floor=excess,
        execution_excess=execution_excess,
        selection_excess=selection_excess,
        advantages=advantages,
        identity_holds=identity_holds,
    )


def execution_excess_only(trajectory: Sequence[Edge]) -> float:
    """Off the designed suite the graph-wide optimum, hence selection regret, is unavailable; only
    per-operation execution excess is reported (each operation against its own floor). Part 3, Section
    2.2, layer 4."""
    total = 0.0
    for e in trajectory:
        if e.actual is None:
            raise ValueError(f"edge {e.label!r} has no actual time")
        total += e.actual - e.floor
    return total


def two_ratios(m_actual: float, floor: float, best_achieved: float) -> dict:
    """The two reported efficiency ratios and the two-sided bracket on the true optimum (Part 3, Section
    2). ``floor <= optimum <= best_achieved``: the floor ratio is against the structural lower bound
    (unbeatable given the weights), the best ratio against the achievable DEA frontier (min makespan over
    observed valid traces)."""
    if floor <= 0:
        raise ValueError("floor must be positive")
    if best_achieved + _EPS < floor:
        raise ValueError(
            "best-achieved makespan is below the floor: a floor weight was over-estimated; revise it down"
        )
    return {
        "floor_ratio": m_actual / floor,
        "best_ratio": m_actual / best_achieved,
        "bracket_low": floor,
        "bracket_high": best_achieved,
    }


def gold_is_refuted(gold_makespan: float, observed_valid_makespans: Sequence[float]) -> Optional[float]:
    """The disclosed gold reference is refutable (Part 3, Section 2.2, layer 2): if any observed valid
    trace reaches the goal with a lower makespan, return the new (lower) best-known makespan to update the
    gold to; otherwise ``None`` (the gold stands)."""
    beats = [m for m in observed_valid_makespans if m + _EPS < gold_makespan]
    return min(beats) if beats else None
