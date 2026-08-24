"""Reproducibility statistics (Part 1, Section 6).

Report distributions and confidence intervals, never a single number. The
geometric mean summarises ratios (Hoefler and Belli 2015). CONFIRM repeats until
the interval is tight enough (Maricq et al. 2018). Two results differ only if
their confidence intervals do not overlap. A non-parametric percentile bootstrap
is provided as the recommended interval; a normal approximation is a convenience.
"""
from __future__ import annotations

import math
import random
from typing import Callable, List, Sequence

from .types import Estimate

# two-sided normal critical values
_Z = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}


def geomean(values: Sequence[float]) -> float:
    """Geometric mean of positive values."""
    vals = list(values)
    if not vals:
        raise ValueError("empty sequence")
    if any(v <= 0 for v in vals):
        raise ValueError("geometric mean needs strictly positive values")
    return math.exp(sum(math.log(v) for v in vals) / len(vals))


def mean_ci(samples: Sequence[float], confidence: float = 0.95) -> Estimate:
    """Normal-approximation CI on the mean (convenience; prefer ``bootstrap_ci``)."""
    xs = list(samples)
    n = len(xs)
    if n == 0:
        raise ValueError("no samples")
    m = sum(xs) / n
    if n == 1:
        return Estimate(m, m, m, 1)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    se = math.sqrt(var / n)
    z = _Z.get(confidence, _Z[0.95])
    return Estimate(m, m - z * se, m + z * se, n)


def bootstrap_ci(samples: Sequence[float], confidence: float = 0.95,
                 iters: int = 2000, seed: int = 0,
                 statistic: Callable[[Sequence[float]], float] = None) -> Estimate:
    """Non-parametric percentile bootstrap CI (Hoefler and Belli style).

    Deterministic given ``seed``. ``statistic`` defaults to the mean.
    """
    xs = list(samples)
    n = len(xs)
    if n == 0:
        raise ValueError("no samples")
    stat = statistic or (lambda v: sum(v) / len(v))
    rng = random.Random(seed)
    boot: List[float] = []
    for _ in range(iters):
        resample = [xs[rng.randrange(n)] for _ in range(n)]
        boot.append(stat(resample))
    boot.sort()
    lo_i = int((1 - confidence) / 2 * iters)
    hi_i = int((1 + confidence) / 2 * iters) - 1
    hi_i = max(lo_i, min(hi_i, iters - 1))
    return Estimate(stat(xs), boot[lo_i], boot[hi_i], n)


def confirm(sampler: Callable[[], float], target_rel_halfwidth: float = 0.01,
            confidence: float = 0.95, min_n: int = 3, max_n: int = 1000,
            batch: int = 1) -> Estimate:
    """Repeat until the CI half-width / mean <= target (Maricq CONFIRM).

    ``sampler`` returns one fresh measurement per call. Stops early when the
    relative half-width target is met, or at ``max_n`` samples.
    """
    xs: List[float] = []
    while len(xs) < max_n:
        for _ in range(max(1, batch)):
            xs.append(sampler())
        if len(xs) < min_n:
            continue
        est = mean_ci(xs, confidence)
        if est.value != 0 and est.half_width / abs(est.value) <= target_rel_halfwidth:
            return est
    return mean_ci(xs, confidence)


def different(a: Estimate, b: Estimate) -> bool:
    """Two results differ iff their confidence intervals do NOT overlap."""
    return not a.overlaps(b)
