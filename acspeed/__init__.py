"""acspeed: a reference implementation of the Part 1 baseline for measuring
agent-cloud operation efficiency as a speed test.

Modules map to the paper's Part 1:
    types          Span / CVector / Estimate               (Sections 2, 3, 6)
    criticalpath   the spine: critical path + owner split  (Section 2)
    capability     delivered-capability normalization      (Section 3)
    discriminator  control- vs data-plane fixed+variable   (Section 3)
    agenttime      raw vs critical agent-time              (Section 4)
    repro          error bars, CONFIRM, non-overlapping CI (Section 6)
    probes         probe output parsers                    (Section 3)
"""
from __future__ import annotations

from . import probes, runners, traceio
from .agenttime import decompose, inference_seconds
from .capability import dci, dominant_axis, normalize_phase, ratios
from .criticalpath import critical_path, is_critical, owner_split, schedule
from .discriminator import fit_fixed_variable, is_constant, karp_flatt, plane_shares
from .repro import bootstrap_ci, confirm, different, geomean, mean_ci
from .types import AGENT, PLATFORM, CVector, Estimate, Span

__version__ = "0.1.0"

__all__ = [
    "Span", "CVector", "Estimate", "AGENT", "PLATFORM",
    "schedule", "critical_path", "owner_split", "is_critical",
    "ratios", "dci", "dominant_axis", "normalize_phase",
    "fit_fixed_variable", "plane_shares", "karp_flatt", "is_constant",
    "decompose", "inference_seconds",
    "geomean", "mean_ci", "bootstrap_ci", "confirm", "different",
    "probes", "runners", "traceio",
    "__version__",
]
