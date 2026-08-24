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

Part 2 (the operation):
    operation      seven-slot schema, three-type typology, registers, milestones
    session        session-as-trace and the excess-vs-optimal efficiency metric
"""
from __future__ import annotations

from . import adapters, operation, probes, runners, session, traceio
from .agenttime import decompose, inference_seconds
from .operation import (
    ACTIVE, APP_SERVING, DEPROVISION, OPERATE_MUTATE, PROVISION, SSH_READY,
    Operation, Phase, has_defined_endpoints, is_schema_conformant, register_of,
)
from .session import Session, efficiency
from .capability import dci, dominant_axis, normalize_phase, ratios
from .criticalpath import critical_path, is_critical, owner_split, schedule
from .discriminator import fit_fixed_variable, fit_is_valid, is_constant, karp_flatt, plane_shares
from .repro import bootstrap_ci, confirm, different, geomean, mean_ci
from .types import AGENT, PLATFORM, CVector, Estimate, Span

__version__ = "0.1.0"

__all__ = [
    "Span", "CVector", "Estimate", "AGENT", "PLATFORM",
    "schedule", "critical_path", "owner_split", "is_critical",
    "ratios", "dci", "dominant_axis", "normalize_phase",
    "fit_fixed_variable", "fit_is_valid", "plane_shares", "karp_flatt", "is_constant",
    "decompose", "inference_seconds",
    "geomean", "mean_ci", "bootstrap_ci", "confirm", "different",
    "Operation", "Phase", "PROVISION", "OPERATE_MUTATE", "DEPROVISION",
    "SSH_READY", "APP_SERVING", "ACTIVE", "register_of",
    "has_defined_endpoints", "is_schema_conformant",
    "Session", "efficiency",
    "probes", "runners", "traceio", "adapters", "operation", "session",
    "__version__",
]
