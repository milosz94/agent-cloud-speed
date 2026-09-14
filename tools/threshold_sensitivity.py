#!/usr/bin/env python3
"""How much does the owner split depend on the 60-second generation-gap threshold?

acspeed.transcript attributes an unlogged gap that ENDS in an agent turn to the agent when it is
shorter than MAX_GEN_GAP (60 s) and to the platform when it is longer, on the reasoning that a
minute of silence before an agent turn is not model generation. The threshold is a judgement, and a
reviewer's fair objection is that duration alone decides it: a 59 s gap is all agent, a 61 s gap all
platform.

That objection is answerable from data already published. The transcripts ship with the artifact and
the threshold is applied when READING them, not baked into them, so the same runs can be re-read at
other thresholds and the movement reported. This measures it, over every published deploy leg, and
reports both the per-cloud agent share at each threshold and whether the cloud ORDERING by agent
share ever changes, which is the thing a cross-cloud claim would actually rest on.

    python3 tools/threshold_sensitivity.py

Runs entirely on the published artifact. No private staging tree, no new runs.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_tables as pt                    # noqa: E402
from acspeed import transcript               # noqa: E402

GRID = [15.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 300.0]
DEFAULT = 60.0


def legs():
    """(cloud, absolute transcript path) for every published deploy leg."""
    out = []
    for cloud in pt.ANON:
        for tier_key, suffix, _name in pt.TIERS:
            celldir = os.path.join(pt.RESULTS, cloud, f"{cloud}-{tier_key}")
            for rec in pt.published_records(cloud, suffix, tier_key):
                rel = (rec.get("deploy") or {}).get("transcript")
                if rel:
                    out.append((cloud, os.path.join(celldir, rel)))
    return out


def main() -> None:
    paths = legs()
    print(f"published deploy legs: {len(paths)}\n")

    table = {}          # threshold -> cloud -> [agent_s, platform_s, wall_s]
    for thr in GRID:
        transcript.MAX_GEN_GAP = thr
        acc = {c: [0.0, 0.0, 0.0] for c in pt.ANON}
        for cloud, path in paths:
            ls = transcript.lane_summary(path, idle_is_platform=True)
            if not ls:
                continue
            acc[cloud][0] += ls["lanes"].get("agent", 0.0)
            acc[cloud][1] += ls["lanes"].get("platform", 0.0)
            acc[cloud][2] += ls.get("wall_s", 0.0)
        table[thr] = acc
    transcript.MAX_GEN_GAP = DEFAULT        # never leave the module mutated

    head = "  ".join(f"{pt.ANON[c]:>7s}" for c in pt.ANON)
    print(f"agent share of wall-clock, by threshold\n\n{'thresh':>7s}  {head}   ordering")
    orderings = set()
    for thr in GRID:
        shares = {c: (100.0 * table[thr][c][0] / table[thr][c][2]) if table[thr][c][2] else 0.0
                  for c in pt.ANON}
        order = tuple(sorted(pt.ANON, key=lambda c: -shares[c]))
        orderings.add(order)
        row = "  ".join(f"{shares[c]:6.2f}%" for c in pt.ANON)
        mark = "  <- default" if thr == DEFAULT else ""
        print(f"{thr:6.0f}s  {row}   {'>'.join(pt.ANON[c] for c in order)}{mark}")

    print()
    for c in pt.ANON:
        vals = [100.0 * table[t][c][0] / table[t][c][2] for t in GRID if table[t][c][2]]
        base = 100.0 * table[DEFAULT][c][0] / table[DEFAULT][c][2]
        print(f"{pt.ANON[c]:6s} agent share ranges {min(vals):.2f}% to {max(vals):.2f}% "
              f"(swing {max(vals)-min(vals):.2f} points; default {base:.2f}%)")

    print()
    print(f"distinct cloud orderings across the whole grid: {len(orderings)}")
    for o in orderings:
        print("   ", " > ".join(pt.ANON[c] for c in o))


if __name__ == "__main__":
    main()
