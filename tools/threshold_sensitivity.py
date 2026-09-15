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

WHAT A "DEPLOY LEG" IS, AND WHY IT IS NOT THE TRANSCRIPT FILE.
An operation ends at its slot-5 readiness signal t1, and the published deploy split is built from the
transcript CLIPPED there, plus one platform-owned boot span from the last in-window event to t1
(autorun.build_op_split, acspeed.transcript.clip_rows). On a Medium run the same session file keeps
growing after t1, because the register, deploy-site-b, integrate and restart operations run inside the
resumed deploy session and are separated by TIME, not by a separate file. Reading the whole file
therefore reads other operations' time into the deploy leg, and does it unevenly across clouds.
Measured before this was corrected: the whole files hold 212,452 s against 95,859 s of deploy leg,
exceeding the leg on 78 of the 94 records, median 1.75x, worst 28.5x.

The window is recovered as first_event_epoch + split.makespan_s, which equals the published
serving_epoch by construction, and `verify_default` proves the recovery by rebuilding every published
split at the adopted 60 s threshold and comparing it to the record. That check is reported, and it can
fail: it currently reproduces 93 of 94.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_tables as pt                    # noqa: E402
from acspeed import transcript               # noqa: E402
from acspeed import operation as acs_op      # noqa: E402
from acspeed.operation import PLATFORM, Span  # noqa: E402

GRID = [15.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 300.0]
DEFAULT = 60.0
TOL = 0.15          # seconds; the records are stored rounded to 0.1


def legs():
    """(cloud, transcript path, serving epoch, published split) for every published deploy leg.

    The serving epoch is the operation's end signal t1. It is recovered from the record rather than
    re-derived: the published makespan is t1 minus the transcript's first event by construction, so
    first_event + makespan_s is t1. `verify_default` is what makes that a checked claim.
    """
    out = []
    for cloud in pt.ANON:
        for tier_key, suffix, _name in pt.TIERS:
            celldir = os.path.join(pt.RESULTS, cloud, f"{cloud}-{tier_key}")
            for rec in pt.published_records(cloud, suffix, tier_key):
                rel = (rec.get("deploy") or {}).get("transcript")
                pub = rec.get("split") or {}
                if not rel or not pub.get("makespan_s"):
                    continue
                path = os.path.join(celldir, rel)
                rows = transcript._load_rows(path)
                if len(rows) < 2:
                    continue
                t1 = transcript._epoch(rows[0]) + pub["makespan_s"]
                out.append((cloud, path, t1, pub, f"{cloud}-{tier_key} run {rec.get('run')}"))
    return out


def deploy_split(path: str, t1: float) -> dict | None:
    """The published deploy leg of one run, at the module's current MAX_GEN_GAP.

    Mirrors autorun.build_op_split: clip the trace at t1, append the platform-owned boot span from the
    last in-window event to t1, read the split off the Part 2 Operation. idle_is_platform is the
    in-window rule (idle while the cloud provisions is platform), and it is applied HERE only because
    the window is the operation, which is the condition that rule is defined under.
    """
    spans = list(transcript.trace_from_transcript(path, until_epoch=t1, idle_is_platform=True))
    if not spans:
        return None
    rows = transcript.clip_rows(transcript._load_rows(path), t1)
    boot = max(0.0, t1 - transcript._epoch(rows[-1])) if rows else 0.0
    if boot > 0.0:
        spans = spans + [Span(id="app_start_boot", duration=boot, owner=PLATFORM,
                              deps=(spans[-1].id,), kind="app_start")]
    op = acs_op.Operation(op_type=acs_op.PROVISION, spans=spans, start_id=spans[0].id,
                          end_id=spans[-1].id, milestones={acs_op.APP_SERVING: spans[-1].id})
    s = op.split()
    return {"critical_platform_s": round(s["critical_platform"], 1),
            "critical_agent_s": round(s["critical_agent"], 1),
            "makespan_s": round(s["wall_clock"], 1),
            "post_handoff_boot_s": round(boot, 1)}


def verify_default(paths) -> list:
    """Rebuild every published split at the adopted threshold and return the records that disagree.

    This is the agreement test between the instrument and the artifact. If it returned an empty list
    unconditionally it would not be a check, so it is reported with its denominator either way.
    """
    transcript.MAX_GEN_GAP = DEFAULT
    bad = []
    for _cloud, path, t1, pub, label in paths:
        got = deploy_split(path, t1)
        keys = ("critical_platform_s", "critical_agent_s", "makespan_s", "post_handoff_boot_s")
        if not got or any(abs(got[k] - pub.get(k, -1e9)) > TOL for k in keys):
            bad.append((label, {k: pub.get(k) for k in keys}, got))
    return bad


def main() -> None:
    paths = legs()
    print(f"published deploy legs: {len(paths)}")

    bad = verify_default(paths)
    print(f"legs whose published split is rebuilt exactly at the {DEFAULT:.0f}s default: "
          f"{len(paths) - len(bad)}/{len(paths)}")
    for label, pub, got in bad:
        print(f"  DISAGREES  {label}\n      record:   {pub}\n      rebuilt:  {got}")
    print()

    table = {}          # threshold -> cloud -> [agent_s, platform_s, makespan_s]
    for thr in GRID:
        transcript.MAX_GEN_GAP = thr
        acc = {c: [0.0, 0.0, 0.0] for c in pt.ANON}
        for cloud, path, t1, _pub, _label in paths:
            sp = deploy_split(path, t1)
            if not sp:
                continue
            acc[cloud][0] += sp["critical_agent_s"]
            acc[cloud][1] += sp["critical_platform_s"]
            acc[cloud][2] += sp["makespan_s"]
        table[thr] = acc
    transcript.MAX_GEN_GAP = DEFAULT        # never leave the module mutated

    head = "  ".join(f"{pt.ANON[c]:>7s}" for c in pt.ANON)
    print(f"agent share of deploy-leg wall-clock, by threshold\n\n{'thresh':>7s}  {head}   ordering")
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
    print(f"deploy-leg wall-clock analysed: {sum(table[DEFAULT][c][2] for c in pt.ANON):,.0f} s")
    print(f"distinct cloud orderings across the whole grid: {len(orderings)}")
    for o in orderings:
        print("   ", " > ".join(pt.ANON[c] for c in o))


if __name__ == "__main__":
    main()
