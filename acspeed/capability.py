"""Delivered-capability normalization (Part 1, Section 3).

``r_axis = measured / reference``; the delivered-capability index ``DCI`` is the
weighted geometric mean of the ratios. The geometric mean is the only summary of
normalized ratios whose ranking is invariant to the choice of reference machine
(Fleming and Wallace 1986). The dominant axis is determined by measurement (the
USE method; Roofline), never a-priori.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

from .types import CVector

AXES = ("compute", "memory", "disk", "network")


def ratios(measured: CVector, reference: CVector) -> Dict[str, float]:
    """Per-axis ratio measured/reference (1.0 == equal to the reference)."""
    m, r = measured.as_dict(), reference.as_dict()
    out = {}
    for a in AXES:
        if r[a] <= 0:
            raise ValueError(f"reference axis {a!r} must be positive")
        out[a] = m[a] / r[a]
    return out


def dci(measured: CVector, reference: CVector,
        weights: Optional[Dict[str, float]] = None) -> float:
    """Weighted geometric mean of the ratios (equal weights by default)."""
    r = ratios(measured, reference)
    if weights is None:
        weights = {a: 1.0 / len(AXES) for a in AXES}
    total_w = sum(weights.get(a, 0.0) for a in AXES)
    if abs(total_w - 1.0) > 1e-9:
        raise ValueError(f"weights must sum to 1, got {total_w}")
    acc = 0.0
    for a in AXES:
        val = r[a]
        if val <= 0:
            raise ValueError(f"ratio for {a!r} must be positive for a geometric mean")
        acc += weights.get(a, 0.0) * math.log(val)
    return math.exp(acc)


def dominant_axis(utilization: Dict[str, float]) -> str:
    """The dominant delivered-capability axis = the most-saturated resource (USE)."""
    if not utilization:
        raise ValueError("empty utilization map")
    return max(sorted(utilization), key=lambda a: utilization[a])


def normalize_phase(duration: float, ratio: float) -> float:
    """Normalize a C-gated phase duration by its dominant-axis ratio."""
    if ratio <= 0:
        raise ValueError("ratio must be positive")
    return duration / ratio
