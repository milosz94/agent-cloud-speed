#!/usr/bin/env python3
"""How much does the owner split depend on the 60-second generation-gap threshold?

acspeed.transcript attributes an unlogged gap that ENDS in an agent turn to the agent when it is
shorter than MAX_GEN_GAP (60 s) and to the platform when it is longer, on the reasoning that a
minute of silence before an agent turn is not model generation. The threshold is a judgement, and a
reviewer's fair objection is that duration alone decides it: a 59 s gap is all agent, a 61 s gap all
platform.

That objection is answerable from data already published. The transcripts ship with the artifact and
the threshold is applied when READING them, not baked into them, so the same runs can be re-read at
other thresholds and the movement reported. This measures it over every published LEG, and reports
both the per-cloud agent share at each threshold and whether the cloud ORDERING by agent share ever
changes, which is the thing a cross-cloud claim would actually rest on.

    python3 tools/threshold_sensitivity.py

Runs entirely on the published artifact. No private staging tree, no new runs.

WHAT THE POPULATION IS, AND WHY IT IS NEITHER THE TRANSCRIPT FILE NOR THE DEPLOY LEG.
Table 5.1's platform and agent columns are a sum over every timed leg of the task: the deploy, then
each scored operation, then each durability cycle (paper_tables._legs). A Medium run is a set of
operations, not one deploy. So the population that a sensitivity on the published split has to cover
is all 359 legs, each on its own declared window:

  deploy            window ends at the readiness signal t1, plus a platform-owned boot span from the
                    last in-window event to t1                       (autorun.build_op_split)
  every other leg   window is the operation's [started_at, verified_at] recorded by the suite engine
                                                                     (autorun.op_window_split)

Two wrong populations were used before. Reading the whole transcript file (this tool until
2026-09-16) charges the gaps BETWEEN operations, which belong to no published leg, under a rule
defined for time inside one: 212,452 s read against 179,704 s of leg. Reading only the 94 deploy legs
(a same-day overcorrection) covers 95,859 s and drops two thirds of the Medium evidence, and it
reverses the answer, because the deploy leg is where the two big clouds are closest.

WHICH OWNER RULE EACH LEG IS READ UNDER.
The owner split was amended mid-study to move idle inside an operation window from the agent to the
platform (Part 5, S3). Runs fall on both sides of that amendment and Table 5.1 pools them, which the
paper discloses. A sensitivity meant to describe the PUBLISHED numbers therefore has to hold each leg
at the rule its own published value was produced under, and vary only the threshold. That is what
`published_rule` recovers, and `verify_default` proves it: rebuilding every leg at the adopted 60 s
reproduces the record on 359 of 359. The check can fail: until v1.2.3 this tool ended every deploy
window at first_event + makespan_s, which is 310.5 s short on the one leg (aws-medium-b run 3) whose
published makespan, under the earlier rule, leaves out held-out idle inside its window, so that leg
matched neither rule. A deploy window now ends at first_event + makespan_s + the time inside it that
neither lane owns (held-out idle, human), taken at its fixed point under each rule (`deploy_end`); on
every other deploy leg that time is zero and the window is unchanged.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy                                                          # noqa: E402

import paper_tables as pt                                            # noqa: E402
from acspeed import gold                                             # noqa: E402
from acspeed import transcript                                       # noqa: E402
from acspeed import operation as acs_op                              # noqa: E402
from acspeed.operation import AGENT, PLATFORM, Span, owner_split     # noqa: E402

GRID = [15.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0, 300.0]
DEFAULT = 60.0
TOL = 0.15          # seconds; the records store splits rounded to 0.1


def legs():
    """Every published leg, as (cloud, label, window, published split).

    The window is ("deploy", path, first_event, makespan_s) or ("op", path, started_at, verified_at).
    A deploy leg's end is resolved per owner rule by `deploy_end`; `published_rule` is what makes the
    resolved window a checked claim rather than an assumption.
    """
    out = []
    for cloud in pt.ANON:
        for tier_key, suffix, _name in pt.TIERS:
            celldir = os.path.join(pt.RESULTS, cloud, f"{cloud}-{tier_key}")
            for rec in pt.published_records(cloud, suffix, tier_key):
                rel = (rec.get("deploy") or {}).get("transcript")
                if not rel:
                    continue
                path = os.path.join(celldir, rel)
                label = f"{cloud}-{tier_key} run {rec.get('run')}"
                pub = rec.get("split")
                if isinstance(pub, dict) and pub.get("makespan_s"):
                    rows = transcript._load_rows(path)
                    if len(rows) >= 2:
                        out.append((cloud, f"{label} deploy",
                                    ("deploy", path, transcript._epoch(rows[0]), pub["makespan_s"]), pub))
                tier_run = rec.get("tier_run") or {}
                items = list(tier_run.get("operations") or [])
                items += list((tier_run.get("durability") or {}).get("cycles") or [])
                for op in items:
                    sp, a, b = op.get("split"), op.get("started_at"), op.get("verified_at")
                    if isinstance(sp, dict) and a is not None and b is not None:
                        out.append((cloud, f"{label} {op.get('op_id')}", ("op", path, a, b), sp))
    return out


def deploy_end(path, first, makespan, idle_is_platform: bool) -> float:
    """The deploy window's end epoch: first event + published makespan + the time inside the window that
    neither lane owns (held-out idle, human), which the published makespan leaves out. Extending the
    window can bring more such time into it, so iterate to the fixed point. On the window each leg is
    published on, that time is nonzero on one of the 94 deploy legs: aws-medium-b run 3, 310.5 s."""
    t1 = first + makespan
    for _ in range(20):
        spans = transcript.trace_from_transcript(path, until_epoch=t1, idle_is_platform=idle_is_platform)
        neither = sum(s.duration for s in spans if s.owner not in (PLATFORM, AGENT))
        nxt = first + makespan + neither
        if abs(nxt - t1) < 1e-6:
            break
        t1 = nxt
    return t1


def resolve(window, idle_is_platform: bool):
    """A leg's window with a deploy leg's end resolved under the given rule."""
    if window[0] == "deploy" and len(window) == 4:
        _kind, path, first, makespan = window
        return ("deploy", path, deploy_end(path, first, makespan, idle_is_platform))
    return window


def rebuild(window, idle_is_platform: bool):
    """One leg's split at the module's current MAX_GEN_GAP, on the window autorun published it on."""
    if window[0] == "deploy":
        _kind, path, t1 = window
        spans = list(transcript.trace_from_transcript(path, until_epoch=t1,
                                                      idle_is_platform=idle_is_platform))
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
                "makespan_s": round(s["wall_clock"], 1)}
    _kind, path, a, b = window
    spans = transcript.trace_from_transcript(path, since_epoch=a, until_epoch=b,
                                             idle_is_platform=idle_is_platform)
    if not spans:
        return None
    s = owner_split(spans)
    owners = s["owners"]
    return {"critical_platform_s": round(owners.get(PLATFORM, {}).get("critical", 0.0), 1),
            "critical_agent_s": round(owners.get(AGENT, {}).get("critical", 0.0), 1),
            "makespan_s": round(s["makespan"], 1)}


def _agrees(got, pub) -> bool:
    return bool(got) and all(abs(got[k] - pub.get(k, -1e9)) <= TOL
                             for k in ("critical_platform_s", "critical_agent_s", "makespan_s"))


def published_rule(all_legs):
    """Per leg, the owner rule whose rebuild reproduces the published value at the adopted threshold.

    On 324 of the 359 legs the two rules give the same answer, so the leg says nothing about which was
    used and either flag is correct for it. The remaining 35 are diagnostic and do say.
    The deploy window is resolved at the adopted threshold under the rule tested and then held fixed: the
    sweep varies the threshold on the window the leg was published on, never the window itself.
    """
    transcript.MAX_GEN_GAP = DEFAULT
    rules, windows, unexplained, both = {}, {}, [], set()
    for i, (_cloud, label, window, pub) in enumerate(all_legs):
        wt, wf = resolve(window, True), resolve(window, False)
        at, af = _agrees(rebuild(wt, True), pub), _agrees(rebuild(wf, False), pub)
        if at and af:
            both.add(i)
        if at:
            rules[i], windows[i] = True, wt
        elif af:
            rules[i], windows[i] = False, wf
        else:
            rules[i], windows[i] = True, wt
            unexplained.append((label, pub, rebuild(wt, True), rebuild(wf, False)))
    return rules, windows, unexplained, both


def main() -> None:
    all_legs = legs()
    kinds = {}
    for _c, label, _w, _p in all_legs:
        kinds["deploy" if label.endswith(" deploy") else "operation"] = \
            kinds.get("deploy" if label.endswith(" deploy") else "operation", 0) + 1
    print(f"published legs: {len(all_legs)}  ({kinds})")

    rules, windows, unexplained, both = published_rule(all_legs)
    print(f"legs rebuilt exactly at the {DEFAULT:.0f}s default: "
          f"{len(all_legs) - len(unexplained)}/{len(all_legs)}")
    for label, pub, got_t, got_f in unexplained:
        print(f"  MATCHES NEITHER OWNER RULE  {label}")
        print(f"      record:          {pub}")
        print(f"      amended rule:    {got_t}")
        print(f"      earlier rule:    {got_f}")
    diag = sum(1 for i in rules if rules[i] is False)
    print(f"legs published under the earlier owner rule (diagnostic): {diag}")
    print(f"legs both rules reproduce (their rule is not recoverable from the record): {len(both)}")
    print()

    table, cells = {}, {}
    for thr in GRID:
        transcript.MAX_GEN_GAP = thr
        acc = {c: [0.0, 0.0] for c in pt.ANON}          # agent_s, makespan_s
        cell = {}
        for i, (cloud, label, _window, _pub) in enumerate(all_legs):
            got = rebuild(windows[i], rules[i])
            if not got:
                continue
            acc[cloud][0] += got["critical_agent_s"]
            acc[cloud][1] += got["makespan_s"]
            k = label.split(" run ")[0]
            cell.setdefault(k, [0.0, 0.0])
            cell[k][0] += got["critical_agent_s"]
            cell[k][1] += got["makespan_s"]
        table[thr] = acc
        cells[thr] = cell
    transcript.MAX_GEN_GAP = DEFAULT                     # never leave the module mutated

    head = "  ".join(f"{pt.ANON[c]:>7s}" for c in pt.ANON)
    print(f"agent share of leg wall-clock, by threshold\n\n{'thresh':>7s}  {head}   ordering")
    orderings = set()
    for thr in GRID:
        shares = {c: (100.0 * table[thr][c][0] / table[thr][c][1]) if table[thr][c][1] else 0.0
                  for c in pt.ANON}
        order = tuple(sorted(pt.ANON, key=lambda c: -shares[c]))
        orderings.add(order)
        row = "  ".join(f"{shares[c]:6.2f}%" for c in pt.ANON)
        mark = "  <- default" if thr == DEFAULT else ""
        print(f"{thr:6.0f}s  {row}   {'>'.join(pt.ANON[c] for c in order)}{mark}")

    print()
    for c in pt.ANON:
        vals = [100.0 * table[t][c][0] / table[t][c][1] for t in GRID if table[t][c][1]]
        base = 100.0 * table[DEFAULT][c][0] / table[DEFAULT][c][1]
        print(f"{pt.ANON[c]:6s} agent share ranges {min(vals):.2f}% to {max(vals):.2f}% "
              f"(swing {max(vals)-min(vals):.2f} points; default {base:.2f}%)")

    print()
    print("per cell (Part 5, section 3): agent share of the cell's legs, lowest and highest over the grid")
    swings = {}
    for k in sorted(cells[DEFAULT]):
        vals = [100.0 * cells[t][k][0] / cells[t][k][1] for t in GRID if cells[t].get(k) and cells[t][k][1]]
        swings[k] = max(vals) - min(vals)
        base = 100.0 * cells[DEFAULT][k][0] / cells[DEFAULT][k][1]
        print(f"  {k:16s} {min(vals):6.2f}% to {max(vals):6.2f}%  (swing {swings[k]:5.2f} points; default {base:.2f}%)")
    lo, hi = min(swings, key=swings.get), max(swings, key=swings.get)
    print(f"  swing across the nine cells: {swings[lo]:.1f} points ({lo}) to {swings[hi]:.1f} ({hi})")

    # The 60 s default cannot tell which rule produced the legs both rules reproduce; the reading above puts
    # them under the amended rule. The two other readings, each leg on its published window:
    for name, rule_of in (("earlier rule on the legs both rules reproduce",
                           lambda i: False if i in both else rules[i]),
                          ("amended rule on every leg", lambda i: True)):
        acc = {}
        for thr in GRID:
            transcript.MAX_GEN_GAP = thr
            for i, (_cloud, label, _window, _pub) in enumerate(all_legs):
                got = rebuild(windows[i], rule_of(i))
                if not got:
                    continue
                k = label.split(" run ")[0]
                a = acc.setdefault(k, {}).setdefault(thr, [0.0, 0.0])
                a[0] += got["critical_agent_s"]
                a[1] += got["makespan_s"]
        transcript.MAX_GEN_GAP = DEFAULT
        sw = {k: max(100.0 * v[t][0] / v[t][1] for t in GRID) - min(100.0 * v[t][0] / v[t][1] for t in GRID)
              for k, v in acc.items()}
        zero = sum(1 for x in sw.values() if x < 0.05)
        print(f"  reading: {name}: swing {min(sw.values()):.1f} to {max(sw.values()):.1f} points; "
              f"zero on {zero} of {len(sw)} cells")
        moved = []
        for k in sorted(acc):
            was = 100.0 * cells[DEFAULT][k][0] / cells[DEFAULT][k][1]
            now = 100.0 * acc[k][DEFAULT][0] / acc[k][DEFAULT][1]
            if abs(now - was) >= 0.05:
                moved.append(f"{k} {was:.1f} to {now:.1f}")
        print(f"      at {DEFAULT:.0f} s the agent share moves on {len(moved)} cells: {'; '.join(moved) or 'none'}")

    # The floor F_C of each cell is the minimum of its deploy legs' critical-platform time (Part 3, S2), so it
    # moves with the threshold too (Part 5, S3): each cell's floor at every grid point, against its value at
    # the adopted 60 s (which is the published floor, to the records' rounding).
    floors = {}
    for thr in GRID:
        transcript.MAX_GEN_GAP = thr
        for i, (_cloud, label, _window, _pub) in enumerate(all_legs):
            if label.endswith(" deploy"):
                got = rebuild(windows[i], rules[i])
                if got:
                    k = label.split(" run ")[0]
                    f = floors.setdefault(k, {})
                    f[thr] = min(f.get(thr, float("inf")), got["critical_platform_s"])
    transcript.MAX_GEN_GAP = DEFAULT
    moves = {k: [100.0 * (f[t] / f[DEFAULT] - 1.0) for t in GRID] for k, f in floors.items()}
    varies = [k for k, v in moves.items() if max(v) - min(v) >= 0.05]
    print()
    print("per cell floor F_C over the grid, percent from its 60 s value")
    for k in sorted(moves):
        print(f"  {k:16s} floor {floors[k][DEFAULT]:7.1f} s   {min(moves[k]):+6.1f}% to {max(moves[k]):+6.1f}%")
    print(f"  the floor moves on {len(varies)} of {len(moves)} cells, by between "
          f"{min(min(v) for v in moves.values()):+.0f} and {max(max(v) for v in moves.values()):+.0f} percent")

    # The ratio interval's upper arm (Table 5.1) is the cell mean of each run's ratio to that floor, so it
    # moves with it: recomputed by gold.part3_provision on copies of the records whose deploy split is the
    # leg re-derived at each grid point.
    deploy_of = {label[:-len(" deploy")]: i for i, (_c, label, _w, _p) in enumerate(all_legs)
                 if label.endswith(" deploy")}
    print()
    print("per cell ratio interval upper arm over the grid (Table 5.1 prints the 60 s value)")
    for cloud in pt.ANON:
        for tier_key, suffix, _name in pt.TIERS:
            recs = pt.published_records(cloud, suffix, tier_key)
            arms = []
            for thr in GRID:
                transcript.MAX_GEN_GAP = thr
                rr = copy.deepcopy(recs)
                for r in rr:
                    i = deploy_of.get(f"{cloud}-{tier_key} run {r.get('run')}")
                    got = rebuild(windows[i], rules[i]) if i is not None else None
                    if got:
                        r["split"].update(got)
                per = (gold.part3_provision(rr) or {}).get("per_run") or []
                if per:
                    arms.append(sum(p["floor_ratio"] for p in per) / len(per))
            transcript.MAX_GEN_GAP = DEFAULT
            if arms:
                print(f"  {cloud + '-' + tier_key:16s} {min(arms):6.2f} to {max(arms):6.2f}  (60 s: {arms[GRID.index(DEFAULT)]:.2f})")

    print()
    print(f"leg wall-clock analysed: {sum(table[DEFAULT][c][1] for c in pt.ANON):,.0f} s")
    print(f"distinct cloud orderings across the whole grid: {len(orderings)}")
    for o in orderings:
        print("   ", " > ".join(pt.ANON[c] for c in o))


if __name__ == "__main__":
    main()
