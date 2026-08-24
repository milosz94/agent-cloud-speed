"""The control-plane vs data-plane fixed-plus-variable discriminator (Part 1,
Section 3), plus the Karp-Flatt serial-fraction falsifier.

Fit ``T(rate) = t_fixed + W * (1 / rate)`` across instances spanning a wide
delivered-capability range. The intercept ``t_fixed`` is the provider
orchestration floor (control-plane, invariant to tenant capability); the slope
``W`` is the machine-gated work (data-plane). This is a mechanistic
fixed-plus-variable model (Hockney; LogP, Culler et al. 1993; Amdahl; and
Mao and Humphrey 2012, who fit exactly this to cloud VM startup), not a
confounded R^2 split.
"""
from __future__ import annotations

from typing import Dict, List, Sequence


def _ols(xs: Sequence[float], ys: Sequence[float]):
    n = len(xs)
    if n < 2:
        raise ValueError("need at least 2 points for a fit")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        raise ValueError("x values must vary (span a capability range)")
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return intercept, slope, r2


def fit_fixed_variable(rates: Sequence[float], times: Sequence[float]) -> Dict[str, float]:
    """Fit ``T = t_fixed + W * (1/rate)``.

    Returns ``t_fixed`` (control-plane floor), ``W`` (data-plane work) and
    ``r_squared``.
    """
    if len(rates) != len(times):
        raise ValueError("rates and times must have equal length")
    if any(r <= 0 for r in rates):
        raise ValueError("rates must be positive")
    xs = [1.0 / r for r in rates]
    intercept, slope, r2 = _ols(xs, list(times))
    return {"t_fixed": intercept, "W": slope, "r_squared": r2}


def fit_is_valid(fit: Dict[str, float], tol: float = 1e-9) -> bool:
    """True iff the fit obeys the model (control-plane floor and data-plane work
    are both non-negative). A negative intercept or slope means the mechanistic
    fixed-plus-variable model is rejected for this phase."""
    return fit["t_fixed"] >= -tol and fit["W"] >= -tol


def plane_shares(fit: Dict[str, float], rate: float) -> Dict[str, float]:
    """At a given delivered rate, split predicted time into control- vs data-plane share.

    Raises if the fit is invalid (a negative floor or slope): per the paper the
    phase should be reported as unresolved rather than given a nonsensical share
    outside [0, 1]. Use ``fit_is_valid`` to check before calling in a sweep.
    """
    if rate <= 0:
        raise ValueError("rate must be positive")
    if not fit_is_valid(fit):
        raise ValueError(
            f"fixed-plus-variable model rejected: t_fixed={fit['t_fixed']:.6g}, "
            f"W={fit['W']:.6g}. A negative control-plane floor or data-plane work is "
            "unphysical; report this phase as unresolved (see the Karp-Flatt falsifier)."
        )
    t_fixed = max(0.0, fit["t_fixed"])   # clamp tiny float-noise negatives to the 0 floor
    data = max(0.0, fit["W"]) / rate
    total = t_fixed + data
    if total <= 0:
        raise ValueError("non-positive predicted time")
    return {"control_plane": t_fixed / total, "data_plane": data / total, "predicted": total}


def karp_flatt(speedups: Sequence[float], processors: Sequence[float]) -> List[float]:
    """Experimentally determined serial fraction ``e = (1/psi - 1/p) / (1 - 1/p)``.

    A value of ``e`` that stays constant as the capability axis grows indicates a
    fixed serial cost; a rising ``e`` indicates growing overhead. This is the
    falsifier for the fixed-plus-variable model (Karp and Flatt, CACM 1990).
    """
    if len(speedups) != len(processors):
        raise ValueError("speedups and processors must have equal length")
    es: List[float] = []
    for psi, p in zip(speedups, processors):
        if p <= 1:
            raise ValueError("processors must be > 1")
        if psi <= 0:
            raise ValueError("speedups must be positive")
        es.append((1.0 / psi - 1.0 / p) / (1.0 - 1.0 / p))
    return es


def is_constant(values: Sequence[float], rel_tol: float = 0.15) -> bool:
    """Rough constancy check: max relative deviation from the mean <= rel_tol."""
    vals = list(values)
    if not vals:
        return True
    m = sum(vals) / len(vals)
    if m == 0:
        return all(abs(v) <= rel_tol for v in vals)
    return max(abs(v - m) for v in vals) / abs(m) <= rel_tol
