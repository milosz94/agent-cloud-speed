#!/usr/bin/env python3
"""Emit the paper's Part 5 figures from the published results tree.

Every quantity plotted here is produced by ``paper_tables``, the same module that emits the LaTeX
bodies of Tables 5.1 and 5.2. That is deliberate and is the whole point of this file: a figure that
computed its own numbers could disagree with the table on the page opposite it, and nothing in the
build would notice. ``--check`` re-derives each figure's inputs and asserts they match the table
values, so a divergence fails loudly instead of printing.

    python3 tools/paper_figures.py --out DIR     # write the PDFs
    python3 tools/paper_figures.py --check       # verify inputs against the tables, plot nothing

Requires matplotlib. ACSPEED_STAGING must point at the per-run record tree, exactly as for
``paper_tables.py``.

Figures are vector PDF, greyscale-safe (every series carries a distinct marker and dash pattern, so
no claim rests on colour) and captionless: the caption belongs to the LaTeX float, not the image.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import OrderedDict, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))            # tools/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

import paper_tables as pt                       # noqa: E402
from acspeed import weighting                   # noqa: E402

# Greyscale-safe series styling. Marker and dash carry the identity; colour is decoration.
STYLE = OrderedDict([
    ("aws",   dict(marker="o", ls="-",  color="#222222", mfc="#222222")),
    ("gcp",   dict(marker="s", ls="--", color="#666666", mfc="#ffffff")),
    ("azure", dict(marker="^", ls=":",  color="#444444", mfc="#bbbbbb")),
])
TIER_ORDER = ["Easy", "Medium (online)", "Medium (disclosed)"]


def _mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.family": "serif",
        "font.size": 9,
        "axes.labelsize": 9,
        "axes.titlesize": 9,
        "legend.fontsize": 8,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "axes.grid": True,
        "grid.alpha": 0.25,
        "grid.linewidth": 0.5,
        "axes.axisbelow": True,
        "figure.dpi": 150,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.02,
    })
    return plt


# ---------------------------------------------------------------------------------------------
# Inputs, all routed through paper_tables so the figures cannot drift from the tables
# ---------------------------------------------------------------------------------------------

def frontier_points(cells):
    """(cloud, E_X seconds, $/hr, on_frontier) exactly as Table 5.2 computes them.

    E_X is the suite total over the cloud's three tier means; the cost coordinate is the mean
    hourly rate over that cloud's EASY cell only. Both conventions are Table 5.2's, not this
    file's."""
    by_cloud = defaultdict(dict)
    for c in cells:
        by_cloud[c["cloud"]][c["tier"]] = c["M_mean"]
    totals = {cl: weighting.suite_total(m) for cl, m in by_cloud.items()}

    runs, cost = [], {}
    for c in cells:
        if c["tier"] != "Easy":
            continue
        rates = [r for r in (pt._hourly(x) for x in c["cost"]) if r is not None]
        if rates:
            cost[c["cloud"]] = sum(rates) / len(rates)
            runs.append(weighting.Run(label=c["cloud"], time=totals[c["cloud"]],
                                      cost=cost[c["cloud"]]))
    front = {r.label for r in weighting.pareto_frontier(runs)} if runs else set()
    return [(cl, totals[cl], cost[cl], cl in front) for cl in pt.ANON if cl in cost]


def schedule_curves(cells):
    """Mean published cost schedule per cloud: {cloud: [(requests_per_month, usd_per_month)]}.

    Only usage-metered runs carry a schedule; standing runs price by the hour and have none, which
    is the paper's own two-kind cost split rather than missing data."""
    acc = defaultdict(lambda: defaultdict(list))
    for c in cells:
        for crr in c["cost"]:
            for lvl in (crr or {}).get("schedule") or []:
                rq, usd = lvl.get("requests_per_month"), lvl.get("usd_per_month")
                if rq is not None and usd is not None:
                    acc[c["cloud"]][rq].append(usd)
    out = {}
    for cl, levels in acc.items():
        out[cl] = sorted((rq, sum(v) / len(v)) for rq, v in levels.items())
    return out


def makespan_series(cells):
    """Per-cell per-run task makespans, the resampling unit behind Table 5.1's CI."""
    return [(c["cloud"], c["tier"], c["Ms"]) for c in cells]


def split_series(cells):
    """Per-cell mean platform / agent / other seconds, the Table 5.1 split columns."""
    return [(c["cloud"], c["tier"], c["platform"], c["agent"], c["idle"]) for c in cells]


# ---------------------------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------------------------

def fig_frontier(cells, out):
    plt = _mpl()
    pts = frontier_points(cells)
    fig, ax = plt.subplots(figsize=(3.4, 2.6))
    for cl, ex, usd, on in pts:
        st = STYLE[cl]
        ax.plot([ex], [usd], marker=st["marker"], color=st["color"],
                mfc=st["color"] if on else "#ffffff", mec=st["color"],
                ms=8, ls="none", mew=1.2,
                label=f"{pt.ANON[cl]}" + (" (frontier)" if on else ""))
        ax.annotate(pt.ANON[cl], (ex, usd), textcoords="offset points",
                    xytext=(7, 4), fontsize=8)
    # the frontier itself: the staircase through non-dominated points
    fr = sorted([(ex, usd) for _, ex, usd, on in pts if on])
    if len(fr) > 1:
        xs, ys = [], []
        for x, y in fr:
            if xs:
                xs.append(x); ys.append(ys[-1])
            xs.append(x); ys.append(y)
        ax.step(xs, ys, where="post", color="#999999", lw=0.8, ls="--", zorder=0)
    ax.set_xlabel(r"suite total critical-path time $E_X$ (s)")
    ax.set_ylabel("standing cost (\\$/hr)")
    # Headroom so no point sits on the frame and no annotation is clipped at the corner.
    xs_all = [p[1] for p in pts]
    ys_all = [p[2] for p in pts]
    ax.set_xlim(0, max(xs_all) * 1.22)
    ax.set_ylim(0, max(ys_all) * 1.18)
    # The fill encodes frontier membership, so it has to be stated somewhere the reader can see.
    from matplotlib.lines import Line2D
    ax.legend(handles=[
        Line2D([], [], ls="none", marker="o", ms=7, mfc="#222222", mec="#222222",
               label="non-dominated"),
        Line2D([], [], ls="none", marker="o", ms=7, mfc="#ffffff", mec="#222222",
               label="dominated"),
    ], frameon=False, loc="lower right", handletextpad=0.4, borderpad=0.2)
    fig.savefig(os.path.join(out, "fig-frontier.pdf"))
    plt.close(fig)
    return "fig-frontier.pdf", pts


def fig_cost_schedule(cells, out):
    plt = _mpl()
    curves = schedule_curves(cells)
    fig, ax = plt.subplots(figsize=(3.4, 2.6))
    for cl in pt.ANON:
        if cl not in curves:
            continue
        st = STYLE[cl]
        xs = [x for x, _ in curves[cl]]
        ys = [y for _, y in curves[cl]]
        ax.plot(xs, ys, marker=st["marker"], ls=st["ls"], color=st["color"],
                mfc=st["mfc"], mec=st["color"], ms=4, lw=1.1, label=pt.ANON[cl])
    ax.set_xscale("log")
    ax.set_xlabel("requests per month")
    ax.set_ylabel("bundle cost (\\$/month)")
    ax.set_ylim(bottom=0)
    ax.legend(frameon=False, loc="upper left")
    fig.savefig(os.path.join(out, "fig-cost-schedule.pdf"))
    plt.close(fig)
    return "fig-cost-schedule.pdf", curves


def fig_makespan(cells, out):
    plt = _mpl()
    series = makespan_series(cells)
    order = [(cl, t) for cl in pt.ANON for t in TIER_ORDER]
    data, labels, pos = [], [], []
    i = 0
    for cl, t in order:
        for c_cl, c_t, Ms in series:
            if c_cl == cl and c_t == t and Ms:
                i += 1
                data.append(Ms)
                labels.append(t.replace("Medium ", "M-").replace("(", "").replace(")", ""))
                pos.append(i + 0.6 * list(pt.ANON).index(cl))
    fig, ax = plt.subplots(figsize=(7.0, 2.7))
    bp = ax.boxplot(data, positions=pos, widths=0.55, patch_artist=True,
                    medianprops=dict(color="#000000", lw=1.2),
                    flierprops=dict(marker=".", ms=3, mfc="#555555", mec="none"))
    k = 0
    for cl in pt.ANON:
        for _ in TIER_ORDER:
            bp["boxes"][k].set(facecolor={"aws": "#dddddd", "gcp": "#ffffff",
                                          "azure": "#bbbbbb"}[cl],
                               edgecolor="#333333", lw=0.8)
            k += 1
    ax.set_xticks(pos)
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel("task makespan $M$ (s)")
    # Headroom: the per-cloud labels sit above the data, so they must not collide with a whisker.
    top = max(max(d) for d in data) * 1.12
    ax.set_ylim(0, top)
    for idx, cl in enumerate(pt.ANON):
        centre = sum(pos[idx * 3:(idx + 1) * 3]) / 3.0
        ax.text(centre, top * 0.95, pt.ANON[cl], ha="center", fontsize=9)
    fig.savefig(os.path.join(out, "fig-makespan.pdf"))
    plt.close(fig)
    return "fig-makespan.pdf", series


def fig_split(cells, out):
    plt = _mpl()
    series = split_series(cells)
    order = [(cl, t) for cl in pt.ANON for t in TIER_ORDER]
    rows = []
    for cl, t in order:
        for c_cl, c_t, p, a, o in series:
            if c_cl == cl and c_t == t and p is not None:
                rows.append((f"{pt.ANON[cl]} {t.replace('Medium ', 'M-').replace('(','').replace(')','')}",
                             p, a, o))
    fig, ax = plt.subplots(figsize=(7.0, 2.7))
    xs = range(len(rows))
    plat = [r[1] for r in rows]
    agent = [r[2] for r in rows]
    other = [r[3] for r in rows]
    ax.bar(xs, plat, 0.6, label="platform", color="#555555", edgecolor="#222222", lw=0.6)
    ax.bar(xs, agent, 0.6, bottom=plat, label="agent", color="#cccccc",
           edgecolor="#222222", lw=0.6)
    ax.bar(xs, other, 0.6, bottom=[p + a for p, a in zip(plat, agent)], label="other",
           color="#ffffff", edgecolor="#222222", lw=0.6, hatch="///")
    ax.set_xticks(list(xs))
    ax.set_xticklabels([r[0] for r in rows], rotation=25, ha="right")
    ax.set_ylabel("critical-path seconds")
    # Headroom: the legend sits inside the axes, so it must clear the tallest stacked bar.
    ax.set_ylim(0, max(p + a + o for p, a, o in zip(plat, agent, other)) * 1.20)
    ax.legend(frameon=False, ncol=3, loc="upper left")
    fig.savefig(os.path.join(out, "fig-split.pdf"))
    plt.close(fig)
    return "fig-split.pdf", rows


# ---------------------------------------------------------------------------------------------

def check(cells) -> list:
    """Assert every figure input equals what the tables publish. Returns a list of problems."""
    bad = []
    # 1. frontier E_X must equal Table 5.2's suite totals, and the frontier set must match
    by_cloud = defaultdict(dict)
    for c in cells:
        by_cloud[c["cloud"]][c["tier"]] = c["M_mean"]
    for cl, ex, usd, on in frontier_points(cells):
        want = weighting.suite_total(by_cloud[cl])
        if abs(ex - want) > 1e-9:
            bad.append(f"frontier E_X for {cl}: figure {ex} vs table {want}")
    # 2. split columns must satisfy Part 1's spine, per cell
    for cl, t, p, a, o in split_series(cells):
        cellrec = next(c for c in cells if c["cloud"] == cl and c["tier"] == t)
        if None in (p, a, o, cellrec["M_mean"]):
            continue
        if abs((p + a + o) - cellrec["M_mean"]) > 0.15:
            bad.append(f"split does not sum to M for {cl} {t}: "
                       f"{p}+{a}+{o} != {cellrec['M_mean']}")
    # 3. makespan series must be the same n the table reports
    for cl, t, Ms in makespan_series(cells):
        cellrec = next(c for c in cells if c["cloud"] == cl and c["tier"] == t)
        if len(Ms) != cellrec["n"]:
            bad.append(f"makespan n for {cl} {t}: figure {len(Ms)} vs table {cellrec['n']}")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".", help="directory to write the PDFs into")
    ap.add_argument("--check", action="store_true",
                    help="verify figure inputs against the tables and exit")
    args = ap.parse_args()

    cells = pt.all_cells()

    problems = check(cells)
    if problems:
        for p in problems:
            print("MISMATCH:", p)
        sys.exit(1)
    print(f"OK: figure inputs agree with the tables across {len(cells)} cells, "
          f"{sum(c['n'] for c in cells)} runs.")
    if args.check:
        return

    os.makedirs(args.out, exist_ok=True)
    for fn in (fig_frontier, fig_cost_schedule, fig_makespan, fig_split):
        name, _ = fn(cells, args.out)
        print("wrote", os.path.join(args.out, name))


if __name__ == "__main__":
    main()
