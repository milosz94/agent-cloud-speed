"""Critical-path analysis of an execution trace (Part 1, Section 2, the spine).

This reuses the classic critical-path method (Kelley and Walker 1959; the
work-span model, Blumofe and Leiserson 1999) and its trace-based instantiation
(The Mystery Machine, OSDI 2014; CRISP, USENIX ATC 2022). Our only addition is
to label critical-path segments by OWNER (agent vs platform) rather than by
service tier.

Model: each span carries a duration and happens-before dependencies. A forward
pass gives each span's earliest finish; the makespan is the maximum. A backward
pass gives slack; a span is on the critical path iff its slack is ~0. The
makespan partitions into per-owner critical time, and
``overlap(owner) = raw(owner) - critical(owner)``.
"""
from __future__ import annotations

from typing import Dict, List, Sequence

from .types import Span

_EPS = 1e-9


def _index(spans: Sequence[Span]) -> Dict[str, Span]:
    by_id: Dict[str, Span] = {}
    for s in spans:
        if s.id in by_id:
            raise ValueError(f"duplicate span id {s.id!r}")
        by_id[s.id] = s
    for s in spans:
        for d in s.deps:
            if d not in by_id:
                raise ValueError(f"span {s.id!r} depends on unknown span {d!r}")
    return by_id


def _toposort(spans: Sequence[Span]):
    indeg = {s.id: 0 for s in spans}
    succ: Dict[str, List[str]] = {s.id: [] for s in spans}
    for s in spans:
        for d in s.deps:
            succ[d].append(s.id)
            indeg[s.id] += 1
    queue = [sid for sid, k in indeg.items() if k == 0]
    order: List[str] = []
    while queue:
        sid = queue.pop()
        order.append(sid)
        for t in succ[sid]:
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    if len(order) != len(spans):
        raise ValueError("dependency cycle detected")
    return order, succ


def schedule(spans: Sequence[Span]) -> dict:
    """Forward and backward CPM passes.

    Returns a dict with ``makespan`` and per-id ``earliest_finish``,
    ``latest_finish`` and ``slack``.
    """
    spans = list(spans)
    if not spans:
        return {"makespan": 0.0, "earliest_finish": {}, "latest_finish": {}, "slack": {}}
    by_id = _index(spans)
    order, succ = _toposort(spans)

    ef: Dict[str, float] = {}
    for sid in order:
        s = by_id[sid]
        start = max((ef[d] for d in s.deps), default=0.0)
        ef[sid] = start + s.duration
    makespan = max(ef.values())

    lf: Dict[str, float] = {}
    for sid in reversed(order):
        if not succ[sid]:
            lf[sid] = makespan
        else:
            lf[sid] = min(lf[t] - by_id[t].duration for t in succ[sid])
    slack = {sid: lf[sid] - ef[sid] for sid in order}
    return {"makespan": makespan, "earliest_finish": ef, "latest_finish": lf, "slack": slack}


def is_critical(spans: Sequence[Span], sched: dict = None) -> Dict[str, bool]:
    """Per-span: True iff the span lies on a critical path (slack ~ 0)."""
    sched = sched or schedule(spans)
    return {sid: abs(sl) <= _EPS for sid, sl in sched["slack"].items()}


def critical_path(spans: Sequence[Span]) -> List[Span]:
    """Return one critical chain, ordered start -> finish.

    The chain is contiguous, so the sum of its span durations equals the
    makespan. Ties are broken deterministically by span id.
    """
    spans = list(spans)
    if not spans:
        return []
    by_id = _index(spans)
    sched = schedule(spans)
    ef = sched["earliest_finish"]
    makespan = sched["makespan"]

    sinks = sorted(sid for sid in ef if abs(ef[sid] - makespan) <= _EPS)
    cur = sinks[0]
    chain: List[Span] = []
    while cur is not None:
        s = by_id[cur]
        chain.append(s)
        start = ef[cur] - s.duration
        preds = sorted(d for d in s.deps if abs(ef[d] - start) <= _EPS)
        cur = preds[0] if preds else None
    chain.reverse()
    return chain


def owner_split(spans: Sequence[Span]) -> dict:
    """Partition the makespan by owner along the critical path.

    Returns ``makespan`` and, per owner, ``raw`` (total effort, may exceed
    wall-clock under concurrency), ``critical`` (duration on the critical path)
    and ``overlap`` (= raw - critical, always >= 0). The critical times across
    owners sum to the makespan.
    """
    spans = list(spans)
    sched = schedule(spans)
    makespan = sched["makespan"]
    chain = critical_path(spans)

    raw: Dict[str, float] = {}
    crit: Dict[str, float] = {}
    for s in spans:
        raw[s.owner] = raw.get(s.owner, 0.0) + s.duration
    for s in chain:
        crit[s.owner] = crit.get(s.owner, 0.0) + s.duration

    owners = set(raw) | set(crit)
    out = {}
    for o in owners:
        r = raw.get(o, 0.0)
        c = crit.get(o, 0.0)
        out[o] = {"raw": r, "critical": c, "overlap": r - c}
    return {"makespan": makespan, "owners": out}
