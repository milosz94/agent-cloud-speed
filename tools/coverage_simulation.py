"""Coverage of every 95-percent-labeled percentile-bootstrap interval Part 5 prints, at this wave's n.

Interval kinds (all percentile bootstrap, all resampling runs within a cell):
  K1  Table 5.1 per-cell mean M: repro.bootstrap_ci defaults (2,000 replicates, seed 0, boot[50], boot[1949])
  K2  section 5's twelve difference intervals: rank_test.boot_diff_ci (10,000, seed 20260907, d[250], d[9749])
  K3  per-cloud E_X intervals: paper_tables._suite_replicates + _pct (10,000, seed 20260907, round-index pct)
  K4  per-cloud G_X intervals, paired with the AWS normalizer (GCP and Azure; AWS's is degenerate at 1)
  K5  E_X difference intervals AWS-GCP, Azure-GCP, AWS-Azure (same replicates as K3)

The released code fixes its seed, so every call of a given shape uses the same resampling indices. They
are reconstructed here by replaying the deposit's own random.Random streams, so each simulated interval is
the one the released code would print for that data (verified below against the deposit on the real data).

Designs (truth = the population mean, or the function of population means the interval estimates):
  normal : each cell's data iid N(cell mean, cell sd), n = the cell's n
  cell   : each cell's data = n draws with replacement from that cell's own published M values
Usage: python tools/coverage_simulation.py NSIM   (Part 5 prints the result of NSIM = 10000)
Needs numpy, which the harness itself does not, e.g. uv run --no-project --with numpy python3 tools/coverage_simulation.py 10000.
Part 5's "about 79 to 93 percent" is the SUMMARY's lowest and highest coverage (K1 cell, GCP Medium (online); K4 normal, GCP).
"""

import glob, json, math, os, random, re, sys, tempfile, time
import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- loader: the published cells, independent of tools/paper_tables.py ----
ROOT = os.path.join(REPO, "results")
CLOUDS = ["aws", "gcp", "azure"]
TIERS = [("easy", "Easy"), ("medium-a", "Medium (online)"), ("medium-b", "Medium (disclosed)")]
ROW = re.compile(r"^\|\s*\[\d+\]\(sessions/([0-9a-f-]+)\.jsonl\)", re.M)


def _M(rec):
    legs = [rec.get("split")]
    tr = rec.get("tier_run") or {}
    legs += [op.get("split") for op in (tr.get("operations") or [])]
    legs += [c.get("split") for c in ((tr.get("durability") or {}).get("cycles") or [])]
    return sum((l.get("makespan_s") or 0.0) for l in legs if isinstance(l, dict))


def _rate(crr):
    if not crr:
        return None
    if crr.get("kind") == "standing" and crr.get("monthly_usd") is not None:
        return crr["monthly_usd"] / 730.0
    te = crr.get("traffic_estimate") or {}
    return te["low"] / 730.0 if te.get("low") is not None else None


def cells():
    out = {}
    for c in CLOUDS:
        for key, name in TIERS:
            d = os.path.join(ROOT, c, f"{c}-{key}")
            uu = ROW.findall(open(os.path.join(d, "README.md")).read())
            idx = {}
            for p in glob.glob(os.path.join(d, "records", "run*.json")):
                r = json.load(open(p))
                for rd in r.get("rounds") or []:
                    if rd.get("session"):
                        idx[rd["session"]] = r
            recs = [idx[u] for u in uu]
            out[(c, name)] = {"M": [m for m in (_M(r) for r in recs) if m],
                              "n": len(recs),
                              "rates": [x for x in (_rate(r.get("cost_run_rate") or {}) for r in recs)
                                        if x is not None]}
    return out


TN = [t for _, t in TIERS]


def counts(idx_rows, n):
    C = np.zeros((len(idx_rows), n))
    for i, row in enumerate(idx_rows):
        for j in row:
            C[i, j] += 1.0
    return C


def k1_counts(n, iters=2000, seed=0):            # repro.bootstrap_ci: xs[rng.randrange(n)]
    rng = random.Random(seed)
    return counts([[rng.randrange(n) for _ in range(n)] for _ in range(iters)], n)


def k2_counts(nx, ny, B=10000, seed=20260907):   # rank_test.boot_diff_ci: choices(x) then choices(y)
    rnd = random.Random(seed)
    ix, iy = [], []
    for _ in range(B):
        ix.append(rnd.choices(range(nx), k=nx))
        iy.append(rnd.choices(range(ny), k=ny))
    return counts(ix, nx), counts(iy, ny)


def suite_counts(cs, B=10000, seed=20260907):    # paper_tables._suite_replicates stream, paired scheme
    rnd = random.Random(seed)
    idx = {k: [] for k in cs}
    for _ in range(B):
        for c in CLOUDS:
            for t in TN:
                n = len(cs[(c, t)]["M"])
                idx[(c, t)].append(rnd.choices(range(n), k=n))
        for c in CLOUDS:                          # cost draw is consumed under both schemes
            nr = len(cs[(c, "Easy")]["rates"])
            if nr:
                rnd.choices(range(nr), k=nr)
    return {k: counts(v, len(cs[k]["M"])) for k, v in idx.items()}


def pct_sorted_idx(B, q):                         # paper_tables._pct: round(q/100*(B-1))
    return min(B - 1, max(0, int(round(q / 100.0 * (B - 1)))))


def kth(a, ks):
    p = np.partition(a, ks, axis=1)
    return [p[:, k] for k in ks]


def verify(cs):
    """Each reconstruction must reproduce the deposit's own interval on the real data."""
    sys.path.insert(0, REPO)
    sys.path.insert(0, os.path.join(REPO, "tools"))
    os.environ.setdefault("ACSPEED_STAGING", tempfile.mkdtemp())
    from acspeed import repro
    import rank_test, paper_tables as pt
    worst = 0.0
    # K1 on the nine cells and on 40 random inputs
    rs = np.random.default_rng(1)
    tests = [np.array(cs[k]["M"]) for k in cs] + [rs.normal(size=n) * 100 for n in (10, 12) for _ in range(20)]
    for x in tests:
        e = repro.bootstrap_ci(list(x))
        b = np.sort(k1_counts(len(x)) @ x / len(x))
        worst = max(worst, abs(b[50] - e.lo), abs(b[1949] - e.hi))
    print("K1 max |mine - repro.bootstrap_ci| over %d inputs: %.2e" % (len(tests), worst))
    # K2 on the twelve published comparisons
    comps = []
    for t in TN:
        for i in range(3):
            for j in range(i + 1, 3):
                comps.append((CLOUDS[i], t, CLOUDS[j], t))
    for c in CLOUDS:
        comps.append((c, TN[1], c, TN[2]))
    w2 = 0.0
    for a, ta, b, tb in comps:
        x, y = cs[(a, ta)]["M"], cs[(b, tb)]["M"]
        lo, hi = rank_test.boot_diff_ci(x, y)
        Cx, Cy = k2_counts(len(x), len(y))
        d = np.sort(Cx @ np.array(x) / len(x) - Cy @ np.array(y) / len(y))
        w2 = max(w2, abs(d[250] - lo), abs(d[9749] - hi))
        print("  K2 %-5s %-18s vs %-5s %-18s [%9.1f, %9.1f]" % (a, ta, b, tb, d[250], d[9749]))
    print("K2 max |mine - rank_test.boot_diff_ci| over 12: %.2e" % w2)
    # K3-K5 against suite_uncertainty
    su = pt.suite_uncertainty(pt.all_cells())
    S = suite_counts(cs)
    mu = {k: S[k] @ np.array(cs[k]["M"]) / len(cs[k]["M"]) for k in cs}
    ex = {c: sum(mu[(c, t)] for t in TN) for c in CLOUDS}
    gx = {c: np.exp(np.mean([np.log(mu[(c, t)] / mu[("aws", t)]) for t in TN], axis=0)) for c in CLOUDS}
    lo_i, hi_i = pct_sorted_idx(10000, 2.5), pct_sorted_idx(10000, 97.5)
    w3 = 0.0
    for c in CLOUDS:
        e, g = np.sort(ex[c]), np.sort(gx[c])
        w3 = max(w3, abs(e[lo_i] - su[c]["ex_ci"][0]), abs(e[hi_i] - su[c]["ex_ci"][1]),
                 abs(g[lo_i] - su[c]["gx_ci"][0]), abs(g[hi_i] - su[c]["gx_ci"][1]))
        print("  E_X %-5s [%.1f, %.1f]  G_X [%.4f, %.4f]" % (c, e[lo_i], e[hi_i], g[lo_i], g[hi_i]))
    for (a, b), v in su["_pairs"].items():
        d = np.sort(ex[a] - ex[b])
        w3 = max(w3, abs(d[lo_i] - v["ci"][0]), abs(d[hi_i] - v["ci"][1]))
        print("  E_X %s - %s [%.1f, %.1f]" % (a, b, d[lo_i], d[hi_i]))
    print("K3-K5 max |mine - suite_uncertainty| : %.2e   (indices %d, %d)" % (w3, lo_i, hi_i))
    return comps, S


def draw(cs, key, design, N, rng):
    x = np.array(cs[key]["M"])
    n = len(x)
    if design == "normal":
        return x.mean() + x.std(ddof=1) * rng.standard_normal((N, n))
    return x[rng.integers(0, n, size=(N, n))]


def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    CH = 250
    cs = cells()
    comps, S = verify(cs)
    truth = {k: float(np.mean(cs[k]["M"])) for k in cs}
    res = []

    def rec(kind, label, design, cov):
        se = math.sqrt(cov * (1 - cov) / N)
        res.append((kind, label, design, cov, se))
        print("%-3s %-44s %-6s %.4f  (MC se %.4f)" % (kind, label, design, cov, se), flush=True)

    # K1, standard normal at n = 10 and 12 (the interval is affine-equivariant, so this is every normal)
    for n in (10, 12):
        C = k1_counts(n)
        rng = np.random.default_rng(100 + n)
        hit = 0
        for s in range(0, N, CH * 4):
            z = rng.standard_normal((min(CH * 4, N - s), n))
            lo, hi = kth(z @ C.T / n, [50, 1949])
            hit += int(np.sum((lo <= 0) & (0 <= hi)))
        rec("K1", "N(0,1) n=%d" % n, "normal", hit / N)
    for design in ("normal", "cell"):
        rng = np.random.default_rng(7 if design == "normal" else 8)
        # K1 per cell
        for k in cs:
            n = len(cs[k]["M"])
            C = k1_counts(n)
            X = draw(cs, k, design, N, rng)
            hit = 0
            for s in range(0, N, CH * 4):
                lo, hi = kth(X[s:s + CH * 4] @ C.T / n, [50, 1949])
                hit += int(np.sum((lo <= truth[k]) & (truth[k] <= hi)))
            rec("K1", "%s %s" % k, design, hit / N)
        # K2 the twelve comparisons
        for a, ta, b, tb in comps:
            ka, kb = (a, ta), (b, tb)
            nx, ny = len(cs[ka]["M"]), len(cs[kb]["M"])
            Cx, Cy = k2_counts(nx, ny)
            X, Y = draw(cs, ka, design, N, rng), draw(cs, kb, design, N, rng)
            tr = truth[ka] - truth[kb]
            hit = 0
            for s in range(0, N, CH):
                d = X[s:s + CH] @ Cx.T / nx - Y[s:s + CH] @ Cy.T / ny
                lo, hi = kth(d, [250, 9749])
                hit += int(np.sum((lo <= tr) & (tr <= hi)))
            rec("K2", "%s %s - %s %s" % (a, ta, b, tb), design, hit / N)
        # K3-K5 the suite bootstrap
        data = {k: draw(cs, k, design, N, rng) for k in cs}
        tex = {c: sum(truth[(c, t)] for t in TN) for c in CLOUDS}
        tgx = {c: math.exp(sum(math.log(truth[(c, t)] / truth[("aws", t)]) for t in TN) / 3) for c in CLOUDS}
        lo_i, hi_i = pct_sorted_idx(10000, 2.5), pct_sorted_idx(10000, 97.5)
        hits = {}
        valid = {}
        nonpos = 0
        for s in range(0, N, CH):
            mu = {k: data[k][s:s + CH] @ S[k].T / len(cs[k]["M"]) for k in cs}
            nonpos += int(sum(np.sum(m <= 0) for m in mu.values()))
            ex = {c: sum(mu[(c, t)] for t in TN) for c in CLOUDS}
            with np.errstate(invalid="ignore", divide="ignore"):
                gx = {c: np.exp(np.mean([np.log(mu[(c, t)] / mu[("aws", t)]) for t in TN], axis=0))
                      for c in CLOUDS}
            for c in CLOUDS:
                lo, hi = kth(ex[c], [lo_i, hi_i])
                hits[("K3", c)] = hits.get(("K3", c), 0) + int(np.sum((lo <= tex[c]) & (tex[c] <= hi)))
                if c != "aws":
                    ok = ~np.any(np.isnan(gx[c]), axis=1)          # a non-positive resampled mean leaves G_X undefined
                    lo, hi = kth(np.where(np.isnan(gx[c]), np.inf, gx[c]), [lo_i, hi_i])
                    hits[("K4", c)] = hits.get(("K4", c), 0) + int(np.sum(ok & (lo <= tgx[c]) & (tgx[c] <= hi)))
                    valid[c] = valid.get(c, 0) + int(np.sum(ok))
            for a, b in (("aws", "gcp"), ("azure", "gcp"), ("aws", "azure")):
                lo, hi = kth(ex[a] - ex[b], [lo_i, hi_i])
                tr = tex[a] - tex[b]
                hits[("K5", a + "-" + b)] = hits.get(("K5", a + "-" + b), 0) + int(np.sum((lo <= tr) & (tr <= hi)))
        print("    non-positive resampled cell means in the %s suite design: %d" % (design, nonpos))
        print("    simulated samples with G_X defined on every replicate:", valid)
        for (kind, lab), h in hits.items():
            rec(kind, lab, design, h / (valid[lab] if kind == "K4" else N))
    print("\nSUMMARY (min / max coverage per kind and design)")
    for kind in ("K1", "K2", "K3", "K4", "K5"):
        for design in ("normal", "cell"):
            v = [r for r in res if r[0] == kind and r[2] == design]
            lo = min(v, key=lambda r: r[3]); hi = max(v, key=lambda r: r[3])
            print("%s %-6s n=%2d  min %.4f (%s)  max %.4f (%s)  max se %.4f" % (
                kind, design, len(v), lo[3], lo[1], hi[3], hi[1], max(r[4] for r in v)))


if __name__ == "__main__":
    t0 = time.time()
    main()
    print("elapsed %.0f s" % (time.time() - t0))
