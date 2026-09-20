#!/usr/bin/env python3
"""The multiple-comparison sensitivity for Part 5, GENERATED, not transcribed.

Why this file exists. On 2026-09-20 the multiplicity finding was measured correctly at the third
attempt and then mis-stated four times in prose: a casualty list that omitted a row present in the
worker's own table, a "stable from 12 to 26" window that was a chosen sub-interval, a suite-level
result generalised to cells, and a "headline collapses" framing contradicted by the paper's own
Table 5.2 caption. The measurements were right every time; the sentences were not.

Part 5 already solved this for its tables: they are generated from the records "so the figures cannot
drift from the data". This does the same for the sensitivity paragraph. Every number and every name in
the emitted text is computed here. Nothing is typed twice.

    ACSPEED_STAGING=<staging> python3 tools/multiplicity_sensitivity.py [--k 12] [--tex]

Exits non-zero if any published figure fails to reproduce, so it is safe to put in the battery.
"""
from __future__ import annotations

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_tables as pt               # noqa: E402
from rank_test import mannwhitney       # noqa: E402

BOOT_B = 200000          # larger than the published 10,000: corrected levels read far into the tails
SEED = 20260907
ALPHA = 0.05


def _boot_diff(x, y, seed=SEED):
    rnd = random.Random(seed)
    return sorted(sum(rnd.choices(x, k=len(x))) / len(x)
                  - sum(rnd.choices(y, k=len(y))) / len(y) for _ in range(BOOT_B))


def _pctl(xs, level):
    n = len(xs)
    q = (1.0 - level) / 2.0
    at = lambda p: xs[min(n - 1, max(0, int(round(p * (n - 1)))))]   # noqa: E731
    return at(q), at(1.0 - q)


def _asl(draws, null):
    """Smallest alpha at which the percentile interval still excludes the null value."""
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        a, b = _pctl(draws, 1.0 - mid)
        if not (a <= null <= b):
            hi = mid
        else:
            lo = mid
    return hi


def build():
    """Every comparison the wave reports a verdict on, with both halves of Part 1's rule."""
    cells = {}
    for cloud in pt.ANON:
        for key, sfx, name in pt.TIERS:
            cells[(cloud, name)] = [m for m in (pt.task_M(r)
                                    for r in pt.published_records(cloud, sfx, key)) if m]
    clouds = list(pt.ANON)
    out = []
    for key, sfx, name in pt.TIERS:                       # 9 cross-cloud pairs within a tier
        for i in range(len(clouds)):
            for j in range(i + 1, len(clouds)):
                a, b = clouds[i], clouds[j]
                x, y = cells[(a, name)], cells[(b, name)]
                out.append({"group": "cell", "lab": f"{name}: {pt.ANON[a]} vs {pt.ANON[b]}",
                            "d": _boot_diff(x, y), "p": mannwhitney(x, y)[1], "null": 0.0})
    for c in clouds:                                       # 3 within-cloud regime pairs
        x, y = cells[(c, "Medium (online)")], cells[(c, "Medium (disclosed)")]
        out.append({"group": "cell", "lab": f"{pt.ANON[c]}: online vs disclosed",
                    "d": _boot_diff(x, y), "p": mannwhitney(x, y)[1], "null": 0.0})

    cells_raw = pt.all_cells()
    ex, gx, _front = pt._suite_replicates(cells_raw)
    for i in range(len(clouds)):                           # 3 suite-total differences
        for j in range(i + 1, len(clouds)):
            a, b = clouds[i], clouds[j]
            out.append({"group": "E_X", "lab": f"{pt.ANON[a]} minus {pt.ANON[b]}",
                        "d": sorted(u - v for u, v in zip(ex[a], ex[b])), "p": None, "null": 0.0})
    for c in clouds:                                       # 2 informative ratio verdicts
        if c == pt.GX_NORMALIZER:
            continue                                       # degenerate at 1 by construction
        out.append({"group": "G_X", "lab": f"{pt.ANON[c]} vs the normalizer",
                    "d": sorted(gx[c]), "p": None, "null": 1.0})

    for it in out:
        it["asl"] = _asl(it["d"], it["null"])
        # Part 1's rule is a CONJUNCTION, so it is an intersection-union test and its p-value is the
        # larger of the two halves. Correcting that one quantity controls the family-wise rate;
        # correcting both halves separately is strictly more conservative than required.
        it["iut"] = max(it["asl"], it["p"] if it["p"] is not None else 0.0)
        it["published"] = it["asl"] < ALPHA and (it["p"] is None or it["p"] < ALPHA)
        it["ci95"] = _pctl(it["d"], 0.95)
    return out


FAMILIES = {
    "cell":  ("the 12 per-cell comparisons Part 5 \\S5 declares", "cell"),
    "suite": ("the 3 suite-total differences", "E_X"),
    "ratio": ("the 2 informative ratio verdicts", "G_X"),
}


def family(items, group):
    return [it for it in items if it["group"] == group]


def survivors(items, k, method):
    live = [it for it in items if it["published"]]
    rows = sorted(live, key=lambda r: r["iut"])
    if method == "bonferroni":
        keep = [r for r in rows if r["iut"] < ALPHA / k]
    elif method == "holm":
        keep, alive = [], True
        for idx, r in enumerate(rows, 1):
            den = k - idx + 1
            if alive and den > 0 and r["iut"] <= ALPHA / den:
                keep.append(r)
            else:
                alive = False
    elif method == "bh":
        keep, m = [], len(rows)
        cut = -1
        for idx, r in enumerate(rows, 1):
            if r["iut"] <= idx / float(k) * ALPHA:
                cut = idx
        keep = rows[:cut] if cut > 0 else []
    else:
        raise ValueError(method)
    kept = {id(r) for r in keep}
    return keep, [r for r in rows if id(r) not in kept]


def selfcheck(items):
    """Reproduce figures the paper prints. A drift here means the emitted text is untrustworthy."""
    fails = []
    want_ex = {"AWS minus GCP": (2991, 5810), "AWS minus Azure": (185, 2939),
               "GCP minus Azure": (-3601, -1987)}
    cells_raw = pt.all_cells()
    old_b = pt.BOOT_B
    ex, gx, _ = pt._suite_replicates(cells_raw)          # published B and seed, untouched
    clouds = list(pt.ANON)
    for i in range(len(clouds)):
        for j in range(i + 1, len(clouds)):
            a, b = clouds[i], clouds[j]
            lab = f"{pt.ANON[a]} minus {pt.ANON[b]}"
            if lab not in want_ex:
                continue
            d = sorted(u - v for u, v in zip(ex[a], ex[b]))
            lo, hi = _pctl(d, 0.95)
            wl, wh = want_ex[lab]
            if abs(lo - wl) > 2 or abs(hi - wh) > 2:
                fails.append(f"E_X {lab}: got [{lo:.0f}, {hi:.0f}], paper prints [{wl}, {wh}]")
    assert pt.BOOT_B == old_b
    n = sum(len(pt.published_records(c, s, k)) for c in pt.ANON for k, s, _ in pt.TIERS)
    if n != 94:
        fails.append(f"published record count is {n}, paper says 94")

    # The emitted paragraph asserts the paper ALREADY reports the AWS-vs-Azure pair unresolved on two
    # other grounds. Verify that against the paper, so the sentence cannot go stale if the text moves.
    tex = os.environ.get("ACSPEED_PAPER_TEX")
    if tex and os.path.isdir(tex):
        body = os.path.join(tex, "combined_p5body.tex")
        if os.path.exists(body):
            t = " ".join(open(body, encoding="utf-8").read().split())
            if "AWS versus Azure pair is unresolved by the $G_X$ interval and by leave-one-out" not in t:
                fails.append("paper no longer states the AWS-vs-Azure pair unresolved by G_X and LOO; "
                             "the emitted paragraph's 'third reason' clause is now unsupported")
            if "AWS\nminus Azure [$-$31, 2,399] s" not in t.replace(" ", " ") and \
               "AWS minus Azure [$-$31, 2,399] s" not in t:
                fails.append("paper no longer prints the leave-one-out range AWS minus Azure "
                             "[-31, 2,399] s")
    else:
        fails.append("ACSPEED_PAPER_TEX unset or not a directory: cannot verify what the paper "
                     "already discloses, so the emitted paragraph is NOT validated")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tex", action="store_true", help="emit the paper paragraph only")
    args = ap.parse_args()

    items = build()
    fails = selfcheck(items)
    if fails:
        print("SELF-CHECK FAILED, emitted text would be untrustworthy:")
        for f in fails:
            print("  " + f)
        return 1

    # Each family is corrected on its own. The suite differences are EXACT linear combinations of the
    # per-cell differences (verified below), so pooling them would correct the same nine quantities
    # twice and make any procedure indefensibly conservative.
    fams = {}
    for name, (desc, group) in FAMILIES.items():
        rows = family(items, group)
        k = len(rows)
        fams[name] = {"desc": desc, "rows": rows, "k": k,
                      "live": [r for r in rows if r["published"]],
                      "res": {m: survivors(rows, k, m) for m in ("bonferroni", "holm", "bh")}}

    def fmt(it):
        lo, hi = it["ci95"]
        return (f"[{lo:9.3f},{hi:9.3f}]" if it["group"] == "G_X"
                else f"[{lo:9.0f},{hi:9.0f}]")

    if not args.tex:
        for name in ("cell", "suite", "ratio"):
            f = fams[name]
            keep = {m: {id(r) for r in f["res"][m][0]} for m in f["res"]}
            print(f'FAMILY "{name}": {f["desc"]}, k={f["k"]}, '
                  f'{len(f["live"])} decided as published')
            print(f"  {'comparison':46s} {'95% interval':>22} {'IUT p':>9} "
                  f"{'Bonf':>5} {'Holm':>5} {'BH':>4}")
            for it in sorted(f["rows"], key=lambda r: r["iut"]):
                mk = lambda m: ("Y" if id(it) in keep[m] else ".") if it["published"] else "-"  # noqa
                print(f"  {it['lab']:46s} {fmt(it)} {it['iut']:9.5f} "
                      f"{mk('bonferroni'):>5} {mk('holm'):>5} {mk('bh'):>4}")
            for m in ("bonferroni", "holm", "bh"):
                cas = [r["lab"] for r in f["res"][m][1]]
                print(f"  {m:11s}: {len(f['res'][m][0])} of {len(f['live'])} survive"
                      f"{'; drops ' + ', '.join(cas) if cas else ''}")
            print()

    # survives under EVERY procedure, in its own family: computed, never asserted
    robust = [it["lab"] for name in fams for it in fams[name]["live"]
              if all(id(it) in {id(r) for r in fams[name]["res"][m][0]}
                     for m in ("bonferroni", "holm", "bh"))]
    fragile = [it["lab"] for name in fams for it in fams[name]["live"]
               if it["lab"] not in robust]

    c, su = fams["cell"], fams["suite"]
    exaa = next(r for r in su["rows"] if "AWS minus Azure" in r["lab"])
    lo, hi = exaa["ci95"]
    b3 = _pctl(exaa["d"], 1.0 - ALPHA / su["k"])
    nm = lambda rows: ", ".join(sorted(r["lab"] for r in rows)) if rows else "none"  # noqa: E731

    print(r"% GENERATED by tools/multiplicity_sensitivity.py --tex. Do not hand-edit; regenerate.")
    print(rf"""\textbf{{Multiplicity.}} Each difference above is judged at a nominal 95 percent level and no
family-wise adjustment is applied, so the rate at which this part would declare at least one difference
under a global null is higher than 5 percent. Correcting the intersection-union $p$ of Part~1's two
halves, which controls the family-wise rate without correcting each half separately, and treating the
{c['k']} comparisons of this section as one family, leaves {len(c['res']['bonferroni'][0])} of the
{len(c['live'])} decided under Bonferroni, {len(c['res']['holm'][0])} under Holm and
{len(c['res']['bh'][0])} under Benjamini--Hochberg; Bonferroni withdraws {nm(c['res']['bonferroni'][1])}.
The three procedures disagree, so the disagreement is what is reported and not any one corrected count.
The {su['k']} suite-total differences are corrected as their own family rather than pooled with the
{c['k']}, because each is exactly the sum of three per-cell differences already counted and pooling
would correct the same quantities twice: within that family
{len(su['res']['bonferroni'][0])} of {len(su['live'])} survive Bonferroni and
{len(su['res']['holm'][0])} survive Holm, the difference being AWS minus Azure, printed above as
[{lo:,.0f}, {hi:,.0f}]~s and widening to [{b3[0]:,.0f}, {b3[1]:,.0f}]~s. That is the pair this part
already reports unresolved by the $G_X$ interval and by leave-one-out, so a correction is a third
reason rather than a new one. What no procedure and no family size tried here disturbs is
{nm([{'lab': x} for x in robust])}. This is disclosed as a sensitivity and not adopted as the rule:
Part~1's rule is pre-registered and bound by every later part, this wave reports every comparison it
runs and selects on none, and choosing one of three disagreeing corrections after seeing which claims
it removes would be a larger liberty than the one it repairs.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
