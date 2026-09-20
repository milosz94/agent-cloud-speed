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


PERM_N = 200000
PERM_SEED = 20260920


def strat_perm_p(blocks, stat="sum", seed=PERM_SEED):
    """Part 1's SECOND half for a statistic that is a SUM (or log-ratio mean) over BLOCKS.

    Part 1 requires a two-sample rank test on the raw runs alongside the difference interval, and
    binds every later part to it. Part 5's per-cell comparisons get Mann-Whitney. The suite summaries
    E_X and G_X are sums over the three tier blocks, and a rank test on a single pooled sample is not
    defined for them, so before 2026-09-20 they carried the interval half alone. Measured under a
    permutation null at these per-cell counts, that half alone runs at roughly 1.7x its nominal rate,
    so the omission was not cosmetic.

    The raw-runs test for a blocked statistic is the randomization test on those blocks: permute run
    labels WITHIN each tier, leaving block structure and per-cell n intact. Part 4 already treats the
    tiers as Fisher blocks ("the block holds the input and the goal identical", citing fisher1935),
    so this is that same design's own test rather than a new instrument.

    blocks: list of (x, y), one per tier, x and y the two clouds' per-run totals in that tier.
    stat:   "sum" for E_X (difference of means, summed over blocks)
            "logratio" for G_X (mean over blocks of log(mean_x / mean_y))
    Returns the two-sided p value. Ties are impossible here (all 94 totals are distinct) but the
    >= comparison is used regardless, which is the conservative direction.
    """
    import math
    rnd = random.Random(seed)

    def value(pairs):
        if stat == "sum":
            return sum(sum(a) / len(a) - sum(b) / len(b) for a, b in pairs)
        if stat == "logratio":
            return sum(math.log((sum(a) / len(a)) / (sum(b) / len(b)))
                       for a, b in pairs) / len(pairs)
        raise ValueError(stat)

    obs = abs(value(blocks))
    pools = [(list(x) + list(y), len(x)) for x, y in blocks]
    hits = 0
    for _ in range(PERM_N):
        drawn = []
        for pool, nx in pools:
            p = pool[:]
            rnd.shuffle(p)
            drawn.append((p[:nx], p[nx:]))
        if abs(value(drawn)) >= obs - 1e-9:
            hits += 1
    return (hits + 1) / float(PERM_N + 1)     # add-one: never reports an impossible p of exactly 0


def _selftest_perm():
    """Two blocks drawn from one distribution must not be separated; a large shift must be."""
    r = random.Random(7)
    same = [([r.gauss(100, 10) for _ in range(10)], [r.gauss(100, 10) for _ in range(10)])
            for _ in range(3)]
    shifted = [([v + 400 for v in a], b) for a, b in same]
    p_same = strat_perm_p(same, "sum")
    p_shift = strat_perm_p(shifted, "sum")
    assert p_same > 0.10, f"null case should not separate, got {p_same}"
    assert p_shift < 0.001, f"shifted case should separate, got {p_shift}"
    return p_same, p_shift


def main() -> None:
    # self-test the exact null before using it
    assert _u_counts(3, 3) == (1, 1, 2, 3, 3, 3, 3, 2, 1, 1), _u_counts(3, 3)
    ps, pd = _selftest_perm()
    print(f"permutation self-test: null p={ps:.3f} (want > 0.10), shifted p={pd:.5f} "
          f"(want < 0.001)")

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

    # ---- the SUITE summaries, which carried the interval half alone before 2026-09-20 ----
    print("\nSuite summaries. Part 1's rule binds these too; its second half here is the "
          "randomization\ntest on the tier blocks (Fisher's randomized-block design, already "
          "Part 4's framing).")
    ex_ci, gx_ci = {}, {}
    cells_raw = pt.all_cells()
    ex_draws, gx_draws, _f = pt._suite_replicates(cells_raw)

    def pctl(xs):
        ys = sorted(xs)
        n = len(ys)
        at = lambda q: ys[min(n - 1, max(0, int(round(q * (n - 1)))))]    # noqa: E731
        return at(0.025), at(0.975)

    clouds = list(pt.ANON)
    print(f"\n{'suite comparison':34s} {'interval on the difference':>28s} {'excl':>6s} "
          f"{'perm p':>9s} {'rule':>10s}")
    for i in range(len(clouds)):
        for j in range(i + 1, len(clouds)):
            a, b = clouds[i], clouds[j]
            d = [u - v for u, v in zip(ex_draws[a], ex_draws[b])]
            lo, hi = pctl(d)
            excl = (lo > 0) or (hi < 0)
            blocks = [(cells[(a, name)], cells[(b, name)]) for _, _, name in pt.TIERS]
            pp = strat_perm_p(blocks, "sum")
            verdict = "DIFFERENT" if (excl and pp < ALPHA) else "undecided"
            print(f"E_X {pt.ANON[a]} minus {pt.ANON[b]:<18s} [{lo:11.0f},{hi:11.0f}] "
                  f"{str(excl):>6s} {pp:9.5f} {verdict:>10s}")
    for c in clouds:
        if c == pt.GX_NORMALIZER:
            continue
        lo, hi = pctl(gx_draws[c])
        excl = not (lo <= 1.0 <= hi)
        blocks = [(cells[(c, name)], cells[(pt.GX_NORMALIZER, name)]) for _, _, name in pt.TIERS]
        pp = strat_perm_p(blocks, "logratio")
        verdict = "DIFFERENT" if (excl and pp < ALPHA) else "undecided"
        print(f"G_X {pt.ANON[c]} vs normalizer{'':<7s} [{lo:11.3f},{hi:11.3f}] "
              f"{str(excl):>6s} {pp:9.5f} {verdict:>10s}")


if __name__ == "__main__":
    main()
