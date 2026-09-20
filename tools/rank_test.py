#!/usr/bin/env python3
"""Apply Part 1's declared decision rule to every difference this wave reports.

Part 1 states one rule and binds every later part to it: a percentile-bootstrap interval on the
DIFFERENCE of two statistics must exclude zero, AND at per-cell n below thirty the difference is
claimed only when a two-sample rank test (Mann-Whitney) on the raw runs agrees. Every cell here is
n = 10 or 12, so the rank test is required on every comparison. Part 5 never reports one.

This runs both halves on the published records and prints where they agree and where they do not.
No new runs; no scipy (the U null is computed exactly by recurrence, and verified at import against
the textbook m = n = 3 distribution).

    python3 tools/rank_test.py
"""
from __future__ import annotations

import os
import random
import sys
from functools import lru_cache

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_tables as pt          # noqa: E402

BOOT_B = 10000
BOOT_SEED = 20260907
ALPHA = 0.05


@lru_cache(maxsize=None)
def _u_counts(m: int, n: int) -> tuple:
    """Exact null distribution of the Mann-Whitney U statistic, as counts indexed by u."""
    if m == 0 or n == 0:
        return (1,)
    a = _u_counts(m - 1, n)
    b = _u_counts(m, n - 1)
    size = m * n + 1
    out = [0] * size
    for u, c in enumerate(a):                 # first sample's element is largest: u shifts by n
        if u + n < size:
            out[u + n] += c
    for u, c in enumerate(b):
        if u < size:
            out[u] += c
    return tuple(out)


def _ranks(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def mannwhitney(x, y):
    """Two-sided Mann-Whitney. Exact when untied, normal approximation with the tie-corrected
    variance when tied. Returns (U, p, method)."""
    m, n = len(x), len(y)
    r = _ranks(list(x) + list(y))
    rx = sum(r[:m])
    u1 = rx - m * (m + 1) / 2.0
    u = min(u1, m * n - u1)
    tied = len(set(list(x) + list(y))) < m + n
    if not tied:
        counts = _u_counts(m, n)
        total = sum(counts)
        p = 2.0 * sum(counts[: int(u) + 1]) / total
        return u, min(1.0, p), "exact"
    import math
    from collections import Counter
    mu = m * n / 2.0
    # Tie-corrected variance (Mann-Whitney with ties): the ordinary m*n*(m+n+1)/12 is reduced by a
    # term in the tie-group sizes. Without it the variance is overstated and p is conservative, so
    # the old "normal+ties" label named a correction the code did not apply.
    N = m + n
    tie_term = sum(t ** 3 - t for t in Counter(list(x) + list(y)).values())
    var = (m * n / 12.0) * ((N + 1) - tie_term / float(N * (N - 1))) if N > 1 else 0.0
    sd = math.sqrt(var) if var > 0 else 0.0
    z = (abs(u1 - mu) - 0.5) / sd if sd else 0.0
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))
    return u, min(1.0, p), "normal+ties"


def boot_diff_ci(x, y, seed=BOOT_SEED):
    """Percentile bootstrap on the difference of means, resampling runs within each sample."""
    rnd = random.Random(seed)
    d = []
    for _ in range(BOOT_B):
        a = sum(rnd.choices(x, k=len(x))) / len(x)
        b = sum(rnd.choices(y, k=len(y))) / len(y)
        d.append(a - b)
    d.sort()
    lo = d[int(0.025 * BOOT_B)]
    hi = d[int(0.975 * BOOT_B) - 1]
    return lo, hi


def main() -> None:
    # self-test the exact null before using it
    assert _u_counts(3, 3) == (1, 1, 2, 3, 3, 3, 3, 2, 1, 1), _u_counts(3, 3)

    cells = {}
    for cloud in pt.ANON:
        for key, sfx, name in pt.TIERS:
            Ms = [m for m in (pt.task_M(r) for r in pt.published_records(cloud, sfx, key)) if m]
            cells[(cloud, name)] = Ms

    comparisons = []
    for key, sfx, name in pt.TIERS:
        cl = list(pt.ANON)
        for i in range(len(cl)):
            for j in range(i + 1, len(cl)):
                comparisons.append((f"{name}: {pt.ANON[cl[i]]} vs {pt.ANON[cl[j]]}",
                                    cells[(cl[i], name)], cells[(cl[j], name)]))
    for cloud in pt.ANON:
        comparisons.append((f"{pt.ANON[cloud]}: Medium online vs disclosed",
                            cells[(cloud, "Medium (online)")], cells[(cloud, "Medium (disclosed)")]))

    print(f"{'comparison':44s} {'n':>7s} {'boot CI on difference':>26s} {'excl 0':>7s} "
          f"{'MW p':>8s} {'sig':>5s} {'rule':>10s}")
    agree = disagree = 0
    for label, x, y in comparisons:
        lo, hi = boot_diff_ci(x, y)
        excl = (lo > 0) or (hi < 0)
        u, p, method = mannwhitney(x, y)
        sig = p < ALPHA
        claimed = excl and sig
        flag = "DIFFERENT" if claimed else ("undecided" if not excl else "BLOCKED")
        if excl == sig:
            agree += 1
        else:
            disagree += 1
        print(f"{label:44s} {len(x):3d}/{len(y):<3d} [{lo:10.0f},{hi:10.0f}] {str(excl):>7s} "
              f"{p:8.4f} {str(sig):>5s} {flag:>10s}")
    print(f"\nbootstrap and rank test agree on {agree} of {agree+disagree} comparisons; "
          f"they disagree on {disagree}.")
    print("BLOCKED = the bootstrap interval excludes zero but the rank test does not agree, so"
          " Part 1's rule forbids claiming the difference.")


if __name__ == "__main__":
    main()
