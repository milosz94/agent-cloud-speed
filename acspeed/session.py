"""Session as a trace of operations, and the efficiency metric (Part 2, Section 7).

A workload is a trace of schema-defined operations on the baseline; every minute is
inside some operation, or is dead time on the critical path between operations.
Efficiency is the excess critical-path wall-clock versus an OPTIMAL reference trace,
and it decomposes, by a clean identity, into:

    excess = selection_excess + execution_excess

where selection_excess is the cost of choosing the wrong or extra operations, and
execution_excess is the cost of running the (matched) operations more slowly. The
critical-path instrument is the same one used within an operation (Part 1), applied
one scale up.

Constructing the optimal reference trace per task is Part 3's job; this module
computes the metric GIVEN an actual and an optimal trace.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple, Union

from .criticalpath import owner_split
from .operation import Operation
from .types import AGENT, PLATFORM

# An operation in a trace is either an Operation or a (label, wall_clock) pair.
OpLike = Union[Operation, Tuple[str, float]]


def _label_and_time(op: OpLike) -> Tuple[str, float]:
    if isinstance(op, Operation):
        return op.label, op.wall_clock()
    label, wc = op
    return label, float(wc)


def _to_totals(ops: Sequence[OpLike]) -> Dict[str, float]:
    """Sum wall-clock by label (an operation may appear more than once in a trace)."""
    totals: Dict[str, float] = {}
    for op in ops:
        label, wc = _label_and_time(op)
        totals[label] = totals.get(label, 0.0) + wc
    return totals


class Session:
    """A trace of operations on the baseline. If the operations' spans are given
    with cross-operation dependencies and globally unique ids, the session's
    wall-clock is the critical path across the whole graph (overlap between
    operations is handled by the same rule used within one)."""

    def __init__(self, operations: Sequence[Operation]) -> None:
        self.operations: List[Operation] = list(operations)

    def spans(self) -> list:
        seen = set()
        out = []
        for op in self.operations:
            for s in op.spans:
                if s.id in seen:
                    raise ValueError(f"duplicate span id {s.id!r} across the session trace")
                seen.add(s.id)
                out.append(s)
        return out

    def wall_clock(self) -> float:
        return owner_split(self.spans())["makespan"]

    def split(self) -> dict:
        owners = owner_split(self.spans())["owners"]
        cp = owners.get(PLATFORM, {}).get("critical", 0.0)
        ca = owners.get(AGENT, {}).get("critical", 0.0)
        return {"critical_platform": cp, "critical_agent": ca, "wall_clock": cp + ca}


def efficiency(actual: Sequence[OpLike], optimal: Sequence[OpLike]) -> dict:
    """Excess critical-path wall-clock of an actual trace versus an optimal one,
    decomposed into selection and execution (Part 2, Section 7).

    ``actual`` and ``optimal`` are sequences of Operation or (label, wall_clock).
    The decomposition is exact: excess == selection_excess + execution_excess.
    """
    A = _to_totals(actual)
    O = _to_totals(optimal)
    actual_total = sum(A.values())
    optimal_total = sum(O.values())
    shared = set(A) & set(O)
    # execution: matched operations that ran slower (or faster) than optimal
    execution_excess = sum(A[l] - O[l] for l in shared)
    # selection: operations present in only one trace (wrong/extra minus missing)
    selection_excess = (sum(A[l] for l in set(A) - set(O))
                        - sum(O[l] for l in set(O) - set(A)))
    excess = actual_total - optimal_total
    ratio = actual_total / optimal_total if optimal_total > 0 else float("inf")
    return {
        "actual": actual_total,
        "optimal": optimal_total,
        "excess": excess,
        "selection_excess": selection_excess,
        "execution_excess": execution_excess,
        "ratio": ratio,
    }
