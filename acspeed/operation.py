"""The operation model (Part 2): the seven-slot schema, the three-type typology,
the three atom registers, and milestone-based decomposition.

An operation is a COMPOSITE over atoms (Part 2, Section 1): a timed, owner-labelled
trace (a list of Spans, as in Part 1) plus the schema slots that make it measurable,
namely start/end signals, phases, milestones, per-phase plane and synchrony, and the
split into critical-platform versus critical-agent time.

Nothing here re-implements the critical path; it reuses Part 1's ``owner_split`` and
``critical_path``. The operation model only adds the Part 2 structure on top, so that
the paper's claims (typology profiles, the register invariant, the reused SSH-ready
milestone split, the falsifiability criteria) are executable rather than prose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .criticalpath import critical_path, owner_split, schedule
from .types import AGENT, PLATFORM, Span

_EPS = 1e-9

# --- operation types (Part 2, Section 6): three infra-plane, state-changing types.
# Read/observe is NOT a fourth type (Section 6.5); it is the observe primitive used
# inside every operation's lifecycle. ---
PROVISION = "provision"
OPERATE_MUTATE = "operate_mutate"
DEPROVISION = "deprovision"
OPERATION_TYPES = (PROVISION, OPERATE_MUTATE, DEPROVISION)

# --- atom registers (Part 2, Section 2). Registers 1+2 are platform, 3 is agent. ---
CONTROL_PLANE = "control_plane"
IN_GUEST = "in_guest"
AGENT_COGNITION = "agent_cognition"
REGISTERS = (CONTROL_PLANE, IN_GUEST, AGENT_COGNITION)
_PLATFORM_REGISTERS = (CONTROL_PLANE, IN_GUEST)

# --- phase axes (Part 2, Section 3, slot 6) ---
CONTROL = "control"    # plane: control-plane-dominated (intercept-dominated)
DATA = "data"          # plane: data-plane-dominated (slope-dominated)
BLOCKING = "blocking"  # synchrony: the call returns on ready
POLL = "poll"          # synchrony: returns on accepted, the caller polls

# --- canonical milestones (Part 2, Section 4) ---
ACTIVE = "active"
SSH_READY = "ssh_ready"
APP_SERVING = "app_serving"


def register_of(span: Span) -> str:
    """Classify a span into one of the three atom registers.

    A span may declare its register in ``kind``; otherwise it is inferred from the
    owner (agent -> agent-cognition; platform -> in-guest data-plane by default).
    """
    if span.kind in REGISTERS:
        return span.kind
    return AGENT_COGNITION if span.owner == AGENT else IN_GUEST


@dataclass
class Phase:
    """A phase of an operation (schema slot 3): a set of spans with a plane and a
    synchrony (slot 6). App materialization is simply a phase with ``plane == DATA``.
    """

    name: str
    span_ids: Tuple[str, ...] = ()
    plane: str = CONTROL
    synchrony: str = BLOCKING

    def __post_init__(self) -> None:
        self.span_ids = tuple(self.span_ids)
        if self.plane not in (CONTROL, DATA):
            raise ValueError(f"plane must be {CONTROL!r} or {DATA!r}, got {self.plane!r}")
        if self.synchrony not in (BLOCKING, POLL):
            raise ValueError(f"synchrony must be {BLOCKING!r} or {POLL!r}, got {self.synchrony!r}")


@dataclass
class Operation:
    """The seven-slot schema (Part 2, Section 3), instantiated over a span trace.

    Slots: (1) start signal ``start_id``; (2) precondition, external to the trace;
    (3) ``phases``; (4) ``milestones`` (name -> the span whose finish reaches it);
    (5) end signal ``end_id``; (6) per-phase plane/synchrony (on each Phase);
    (7) the split into critical-platform vs critical-agent (``split``).
    """

    op_type: str
    spans: Tuple[Span, ...]
    start_id: Optional[str] = None
    end_id: Optional[str] = None
    phases: Tuple[Phase, ...] = ()
    milestones: Dict[str, str] = field(default_factory=dict)
    label: Optional[str] = None

    def __post_init__(self) -> None:
        if self.op_type not in OPERATION_TYPES:
            raise ValueError(f"op_type must be one of {OPERATION_TYPES}, got {self.op_type!r}")
        self.spans = tuple(self.spans)
        self.phases = tuple(self.phases)
        if self.label is None:
            self.label = self.op_type

    # --- slot 5 + slot 7 ---
    def wall_clock(self) -> float:
        return owner_split(self.spans)["makespan"]

    def split(self) -> dict:
        """Critical-platform vs critical-agent time (Part 1's spine). The two
        critical times sum to the wall-clock."""
        owners = owner_split(self.spans)["owners"]
        cp = owners.get(PLATFORM, {}).get("critical", 0.0)
        ca = owners.get(AGENT, {}).get("critical", 0.0)
        overlap = sum(v["overlap"] for v in owners.values())
        return {"critical_platform": cp, "critical_agent": ca,
                "overlap": overlap, "wall_clock": cp + ca}

    def profile(self) -> str:
        """The characteristic profile that distinguishes the typology (Section 6):
        provision is platform-bound, operate/mutate is agent-bound, deprovision is
        control-plane-only. Derived from the measured split and the phase planes,
        not declared."""
        s = self.split()
        cp, ca = s["critical_platform"], s["critical_agent"]
        has_data_phase = any(p.plane == DATA for p in self.phases)
        if not has_data_phase and cp >= ca:
            return "control-plane-only"
        return "platform-bound" if cp >= ca else "agent-bound"

    # --- slot 6: registers (Section 2) ---
    def register_split(self) -> dict:
        raw = {r: 0.0 for r in REGISTERS}
        crit = {r: 0.0 for r in REGISTERS}
        chain = {s.id for s in critical_path(self.spans)}
        for s in self.spans:
            r = register_of(s)
            raw[r] += s.duration
            if s.id in chain:
                crit[r] += s.duration
        return {"raw": raw, "critical": crit}

    def register_invariant_holds(self) -> bool:
        """Registers 1+2 map to platform, register 3 to agent (Section 2). A span
        whose declared register implies an owner different from its own is exactly
        the 'genuine fourth atom register' falsifier (Section 8): report it."""
        for s in self.spans:
            r = register_of(s)
            implied_owner = AGENT if r == AGENT_COGNITION else PLATFORM
            if s.owner not in (AGENT, PLATFORM) or s.owner != implied_owner:
                return False
        return True

    # --- slot 4: milestone-based decomposition (Section 4, the SSH-ready reuse) ---
    def milestone_split(self, milestone: str = SSH_READY) -> dict:
        """Split the critical path at a milestone into a lower half (start ->
        milestone) and an upper half (milestone -> end), each with its own
        platform/agent split. This is the reused-lower-half / novel-upper-half
        decomposition of Section 4."""
        if milestone not in self.milestones:
            raise ValueError(f"no milestone named {milestone!r}")
        sched = schedule(self.spans)
        ef = sched["earliest_finish"]
        m_id = self.milestones[milestone]
        if m_id not in ef:
            raise ValueError(f"milestone span {m_id!r} not in trace")
        m_time = ef[m_id]
        lower = {AGENT: 0.0, PLATFORM: 0.0}
        upper = {AGENT: 0.0, PLATFORM: 0.0}
        for s in critical_path(self.spans):
            bucket = lower if ef[s.id] <= m_time + _EPS else upper
            bucket[s.owner] = bucket.get(s.owner, 0.0) + s.duration
        return {
            "milestone": milestone,
            "milestone_time": m_time,
            "lower": {"critical_platform": lower[PLATFORM], "critical_agent": lower[AGENT]},
            "upper": {"critical_platform": upper[PLATFORM], "critical_agent": upper[AGENT]},
        }


def has_defined_endpoints(op: Operation) -> bool:
    """Falsifiability criterion (Section 8): an operation with no definable start or
    end signal is not a schema-conformant operation."""
    ids = {s.id for s in op.spans}
    return op.start_id in ids and op.end_id in ids


def is_schema_conformant(op: Operation) -> bool:
    """The two executable falsifiers of Section 8: definable endpoints, and every
    span attributable to platform or agent (no unmodelled fourth register)."""
    return has_defined_endpoints(op) and op.register_invariant_holds()
