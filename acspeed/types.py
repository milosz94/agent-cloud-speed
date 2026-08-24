"""Core data types for the acspeed reference implementation.

Part 1 of the paper. A ``Span`` is a segment of an execution trace; every
segment is owned by the ``agent`` or the ``platform`` (Section 2, the spine).
A ``CVector`` is the measured delivered-capability vector (Section 3). An
``Estimate`` carries a value with a confidence interval (Section 6).
"""
from __future__ import annotations

from dataclasses import dataclass

AGENT = "agent"
PLATFORM = "platform"


@dataclass(frozen=True)
class Span:
    """A segment of work in an execution trace.

    Attributes:
        id: unique span identifier.
        duration: seconds (>= 0).
        owner: who is responsible for the segment, e.g. ``"agent"`` / ``"platform"``.
        deps: ids of spans that must FINISH before this span starts (the
            happens-before edges of the dependency DAG used for critical-path
            analysis). Not OpenTelemetry parent/child containment.
        kind: optional label, e.g. inference/orchestration/wait/rework (agent)
            or provision/boot/attach/deploy (platform).
    """

    id: str
    duration: float
    owner: str
    deps: tuple = ()
    kind: str = ""

    def __post_init__(self) -> None:
        if self.duration < 0:
            raise ValueError(f"span {self.id!r} has negative duration {self.duration}")
        # normalise deps to an immutable tuple regardless of input iterable
        object.__setattr__(self, "deps", tuple(self.deps))


@dataclass(frozen=True)
class CVector:
    """Measured delivered-capability vector (throughput / latency axes).

    Values are MEASURED delivered capability, not nominal spec. Capacity
    (RAM/disk GB) is a disclosed constraint and deliberately not an axis here.
    """

    compute: float
    memory: float
    disk: float
    network: float

    def as_dict(self) -> dict:
        return {
            "compute": self.compute,
            "memory": self.memory,
            "disk": self.disk,
            "network": self.network,
        }


@dataclass(frozen=True)
class Estimate:
    """A point estimate with a confidence interval ``[lo, hi]``."""

    value: float
    lo: float
    hi: float
    n: int = 0

    def overlaps(self, other: "Estimate") -> bool:
        """Two estimates are statistically indistinguishable iff their CIs overlap."""
        return not (self.hi < other.lo or other.hi < self.lo)

    @property
    def half_width(self) -> float:
        return (self.hi - self.lo) / 2.0
