"""Agent-time decomposition (Part 1, Section 4).

Raw agent-time is the sum of all agent spans (total effort). Critical agent-time
is the subset on the critical path (the contribution to wall-clock that a user
feels). ``overlap = raw - critical``. Components are labelled by kind:
inference / orchestration / wait / rework. Inference seconds follow the
standardized MLPerf metrics, ``TTFT + TPOT * (output_tokens - 1)``: TTFT already
prices the first token, so TPOT prices only the tokens generated after it.
"""
from __future__ import annotations

from typing import Sequence

from .criticalpath import critical_path, owner_split
from .types import AGENT, PLATFORM, Span

INFERENCE = "inference"
ORCHESTRATION = "orchestration"
WAIT = "wait"
REWORK = "rework"
OTHER = "other"
AGENT_KINDS = (INFERENCE, ORCHESTRATION, WAIT, REWORK)
# component buckets include a catch-all so the parts always sum to the total
COMPONENT_KINDS = (INFERENCE, ORCHESTRATION, WAIT, REWORK, OTHER)


def inference_seconds(ttft: float, tpot: float, output_tokens: int) -> float:
    """Model-generation latency = TTFT + TPOT * (output_tokens - 1) (MLPerf TTFT/TPOT).

    The ``- 1`` is the definition, not an off-by-one: MLPerf's TTFT is the latency
    to the FIRST output token, and TPOT is the inter-token time over the tokens
    generated after it, so pricing all ``n`` tokens at TPOT double-counts the first.
    A single-token generation therefore costs exactly TTFT, and a generation that
    produced no tokens costs nothing (there was no first token to wait for).
    Mirrors Part 1 Eq. (agenttime); keep the two in step.
    """
    if ttft < 0 or tpot < 0 or output_tokens < 0:
        raise ValueError("inference parameters must be non-negative")
    if output_tokens == 0:
        return 0.0
    return ttft + tpot * (output_tokens - 1)


def decompose(spans: Sequence[Span], agent: str = AGENT, platform: str = PLATFORM) -> dict:
    """Full agent-time decomposition for a trace.

    Returns raw / critical / overlap agent-time (seconds), per-kind component
    breakdowns for both raw and critical, the makespan, and the critical
    platform time for convenience.
    """
    spans = list(spans)
    split = owner_split(spans)
    chain_ids = {s.id for s in critical_path(spans)}

    raw_components = {k: 0.0 for k in COMPONENT_KINDS}
    crit_components = {k: 0.0 for k in COMPONENT_KINDS}
    raw_total = 0.0
    crit_total = 0.0
    for s in spans:
        if s.owner != agent:
            continue
        bucket = s.kind if s.kind in raw_components else OTHER  # unknown/empty kinds -> "other"
        raw_total += s.duration
        raw_components[bucket] += s.duration
        if s.id in chain_ids:
            crit_total += s.duration
            crit_components[bucket] += s.duration

    crit_platform = split["owners"].get(platform, {}).get("critical", 0.0)
    return {
        "makespan": split["makespan"],
        "raw_agent_time": raw_total,
        "critical_agent_time": crit_total,
        "overlap_agent_time": raw_total - crit_total,
        "critical_platform_time": crit_platform,
        "raw_components": raw_components,
        "critical_components": crit_components,
    }
