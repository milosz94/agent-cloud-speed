#!/usr/bin/env python3
"""The Medium tier's elapsed span beside the paper's M, and what M leaves out (Part 5, section 4).

M is the per-task critical-path total (paper_tables.task_M). The elapsed span is the task from its session's first
transcript event to its last verification. Their difference is split into the parts the records can name:

    t1 -> first window   from the deploy leg's end (its makespan, which ends at t1) to the next operation's window;
                         the deploy session goes on after the app serves and that work lies in no timed leg
    between cycles       gaps between one restart cycle's verification and the next cycle's start
    window edges         each split leg's declared window less its measured makespan
    unsplit windows      windows with no split (the harness's deploy check)

and the parts are checked to sum to the difference. It then prints the cell means, the regime gaps and the suite
totals on both quantities, and the two claimed verdicts the paper says are decided on M but not on the elapsed span,
with the paper's own interval and rank test (tools/rank_test.py).

    python3 tools/elapsed_span.py          # ACSPEED_STAGING as for paper_tables.py
"""
import datetime as dt
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_tables as pt  # noqa: E402
import rank_test as rt  # noqa: E402

PARTS = ("t1 -> first window", "between cycles", "window edges", "unsplit windows")


def first_event(path):
    """The session's first transcript timestamp, epoch seconds."""
    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                ts = json.loads(line).get("timestamp")
            except ValueError:
                continue
            if ts:
                return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    return None


def windows(rec):
    tr = rec["tier_run"]
    return sorted(list(tr["operations"]) + list(tr["durability"]["cycles"]), key=lambda w: w["started_at"])


def run_parts(rec, t0):
    """(M, elapsed, {part: seconds}) for one Medium record whose session starts at t0."""
    wins = windows(rec)
    m = pt.task_M(rec)
    elapsed = max(w["verified_at"] for w in wins) - t0
    cycles = sorted(rec["tier_run"]["durability"]["cycles"], key=lambda w: w["started_at"])
    parts = {
        "t1 -> first window": wins[0]["started_at"] - (t0 + rec["split"]["makespan_s"]),
        "between cycles": sum(max(0.0, b["started_at"] - a["verified_at"]) for a, b in zip(cycles, cycles[1:])),
        "window edges": sum(w["wall_s"] - w["split"]["makespan_s"] for w in wins if w.get("split")),
        "unsplit windows": sum(w.get("wall_s") or 0.0 for w in wins if not w.get("split")),
    }
    return m, elapsed, parts


def medium_runs():
    """{(cloud, tier_key): [(M, elapsed, parts, agent_after_serving_s)]} over the published Medium runs."""
    out = {}
    for cloud in ("aws", "gcp", "azure"):
        for key, sfx, _name in pt.TIERS:
            if key == "easy":
                continue
            for u, rec in zip(pt.published_uuids(cloud, key), pt.published_records(cloud, sfx, key)):
                t0 = first_event(os.path.join(pt.RESULTS, cloud, "%s-%s" % (cloud, key), "sessions", u + ".jsonl"))
                m, el, parts = run_parts(rec, t0)
                out.setdefault((cloud, key), []).append((m, el, parts, rec["serving"].get("agent_after_serving_s") or 0.0))
    return out


def main():
    runs = medium_runs()
    every = [r for v in runs.values() for r in v]
    excl = sum(el - m for m, el, _p, _a in every)
    total = {k: sum(p[k] for _m, _el, p, _a in every) for k in PARTS}
    print("Medium runs: %d; elapsed span exceeds M by %.0f s, %.1f percent of the elapsed span"
          % (len(every), excl, 100 * excl / sum(el for _m, el, _p, _a in every)))
    for k in PARTS:
        print("  %-20s %9.0f s" % (k, total[k]))
    print("  parts sum to %.0f s (difference %.1f s); agent still acting after t1: %.0f s"
          % (sum(total.values()), sum(total.values()) - excl, sum(a for *_x, a in every)))
    print("\ncell (mean per run)        M   elapsed  t1->window  after t1")
    for (cloud, key), v in sorted(runs.items()):
        print("%-6s %-10s %8.0f %9.0f %11.0f %9.0f" % (cloud, key, st.mean(r[0] for r in v), st.mean(r[1] for r in v),
                                                    st.mean(r[2]["t1 -> first window"] for r in v), st.mean(r[3] for r in v)))
    print("\nregime gap (online minus disclosed) and suite total (Easy cells at their M)")
    for cloud in ("aws", "gcp", "azure"):
        a, b = runs[(cloud, "medium-a")], runs[(cloud, "medium-b")]
        easy_sfx = [s for k, s, _n in pt.TIERS if k == "easy"][0]
        easy = st.mean(pt.task_M(r) for r in pt.published_records(cloud, easy_sfx, "easy"))
        for i, name in ((0, "M"), (1, "elapsed")):
            print("%-6s %-8s gap %6.0f s   suite %6.0f s" % (cloud, name, st.mean(r[i] for r in a) - st.mean(r[i] for r in b),
                                                           easy + st.mean(r[i] for r in a) + st.mean(r[i] for r in b)))
    print("\nverdicts decided on M, not on the elapsed span (bootstrap interval on the difference; rank test)")
    for label, x, y in (("Azure Medium (online) vs Medium (disclosed)", ("azure", "medium-a"), ("azure", "medium-b")),
                        ("GCP vs Azure, Medium (online)", ("gcp", "medium-a"), ("azure", "medium-a"))):
        for i, name in ((0, "M"), (1, "elapsed")):
            xs, ys = [r[i] for r in runs[x]], [r[i] for r in runs[y]]
            lo, hi = rt.boot_diff_ci(xs, ys)
            _u, p, _kind = rt.mannwhitney(xs, ys)
            print("%-44s %-8s [%.0f, %.0f] s  p = %.4f" % (label, name, lo, hi, p))


if __name__ == "__main__":
    main()
