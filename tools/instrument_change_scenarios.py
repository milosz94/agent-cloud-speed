#!/usr/bin/env python3
"""Which cloud verdicts hold under every records-based adjustment for the instrument changes inside the wave?

The published wave (94 runs, 2026-08-29 to 2026-09-07) was measured while the harness changed: the integrate
checker's address rule (2026-09-01), the liveness predicate and owner split (2026-09-04), the integrate visit
retry and browser engine (2026-09-05), and the prompts' hostname-naming instructions (2026-09-01). This tool
re-derives every verdict Part 5 publishes (the twelve per-tier comparisons under both halves of Part 1's rule,
the three suite E_X pairs, the agree? column and frontier membership at the published point) under scenarios
that each size one change from the published records, and reports, per verdict, whether it keeps its
published direction (or its published "undecided") in every scenario where it can be estimated.

It builds on the released tools and changes none of them: tools/paper_tables.py (records, M, the suite
bootstrap, the frontier), tools/rank_test.py (the per-tier bootstrap and the exact rank test). Every adjustment
is computed from a published record or a published session transcript; nothing is typed in by hand.

    ACSPEED_STAGING=<empty dir> python3 tools/instrument_change_scenarios.py            # tables
    ACSPEED_STAGING=<empty dir> python3 tools/instrument_change_scenarios.py --json F   # also write F

No new runs, no cloud access, standard library only.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from itertools import combinations

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import paper_tables as pt          # noqa: E402
import rank_test as rt             # noqa: E402
from acspeed import transcript as T, weighting  # noqa: E402

CLOUDS = list(pt.ANON)                       # aws, gcp, azure
TIERS = [(key, sfx, name) for key, sfx, name in pt.TIERS]
EASY, ONLINE, DISCL = "Easy", "Medium (online)", "Medium (disclosed)"
MIN_N = 5                                    # Part 5: no cell is published below five
ALPHA = rt.ALPHA

REPROMPT = "YOUR PREVIOUS ATTEMPT DID NOT SATISFY THE CHECK"
LOST = "AFTER YOUR LAST RESTART these did NOT survive"

# ------------------------------------------------------------------------------------------------
# Loading: one dict per published run, joined by session UUID exactly as paper_tables does
# ------------------------------------------------------------------------------------------------


def _text(row: dict) -> str:
    c = (row.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    return "\n".join(b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _integrate_attempts(rec: dict, rows: list) -> dict:
    """The integrate operation's rejected attempts, read from the session transcript.

    The suite engine (acspeed/suite.py run_tier) re-prompts a rejected integrate attempt with
    'YOUR PREVIOUS ATTEMPT DID NOT SATISFY THE CHECK: <detail>'. The checker's own wait for a rejected
    attempt runs from the end of the agent's turn to that re-prompt, and it sits inside the leg's makespan
    because the leg is timed first-to-last transcript event in [started_at, verified_at]
    (autorun.py op_window_split). Background-task notifications delivered together with the re-prompt
    (within 2 s before it) belong to the delivery, not to the agent's turn."""
    ops = {o["op_id"]: o for o in rec["tier_run"]["operations"]}
    integ = ops["integrate"]
    w = T.clip_rows(rows, integ["verified_at"], integ["started_at"])
    out = []
    for i, row in enumerate(w):
        if row.get("type") != "user":
            continue
        t = _text(row)
        if REPROMPT not in t:
            continue
        m = re.search(re.escape(REPROMPT) + r": (.*?)\. Fix exactly", t, re.S)
        detail = m.group(1) if m else ""
        j = i - 1
        while j > 0 and w[j].get("type") == "user" and T._epoch(w[j]) >= T._epoch(row) - 2.0:
            j -= 1
        end_prev = T._epoch(w[j])
        if "missing umami wiring" in detail:
            kind = "address"
        elif "[visit]" in detail and "did not register" in detail:
            kind = "visit"
        else:
            kind = "other"
        eng = re.search(r"\((chromium-cli|playwright)\)", detail)
        out.append({"kind": kind, "detail": detail[:160], "engine": eng.group(1) if eng else None,
                    "turn_end": end_prev, "reprompt": T._epoch(row),
                    "wait_s": T._epoch(row) - end_prev})
    leg_first, leg_last = (T._epoch(w[0]), T._epoch(w[-1])) if w else (None, None)
    return {"attempts": out, "leg_first": leg_first, "leg_last": leg_last,
            "leg_makespan_rec": (integ.get("split") or {}).get("makespan_s"),
            "accepted": bool(integ.get("verify_ok"))}


def _cycles_lost(rec: dict, rows: list) -> list:
    """For each durability cycle after the first, the list the harness handed the agent as lost."""
    cyc = rec["tier_run"]["durability"]["cycles"]
    lost = []
    for c in cyc[1:]:
        w = T.clip_rows(rows, c["verified_at"], c["started_at"])
        items = None
        for row in w:
            t = _text(row)
            if row.get("type") == "user" and LOST in t:
                m = re.search(re.escape(LOST) + r": (.*?)\. The deployment", t)
                items = [x.strip() for x in m.group(1).split(",")] if m else ["?"]
                break
        lost.append({"lost": items, "makespan_s": c["split"]["makespan_s"]})
    return lost


def _first_under_500_s(serving: dict):
    """The time the ORIGINAL predicate ('first response with HTTP status < 500') would have stopped the
    clock, read from the fields the durable-serve records carry: the rejected edge responses (origin) and
    the poll log. None where the record carries no poll-level evidence."""
    ts = []
    for o in serving.get("origin") or []:
        try:
            if 0 < int(o.get("code") or 0) < 500:
                ts.append(float(o["t_s"]))
        except (TypeError, ValueError):
            pass
    for p in serving.get("poll_log") or []:
        try:
            if 0 < int(p.get("code") or 0) < 500:
                ts.append(float(p["t_s"]))
        except (TypeError, ValueError):
            pass
    if serving.get("first_serve_at_s") is not None:
        ts.append(float(serving["first_serve_at_s"]))
    return min(ts) if ts else None


_STATUS_200 = re.compile(r"(?<![0-9.])200(?![0-9])")
_NOT_THE_APP = ("Web App - Unavailable",)        # the platform's stopped-site page is not the app serving


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    return "\n".join(x.get("text", "") for x in (c or []) if isinstance(x, dict))


def observed_serve_after_stop(rec: dict, rows: list):
    """For a run whose clock the original predicate stopped on a non-200, the first moment after that stop at which
    the agent's own tool output shows the app answering: the result of a tool call aimed at the app's host with a line
    that carries a 200 and is not the platform's stopped-site page. Returns (stop_epoch, serve_epoch) in transcript
    time, the stop anchored as the deploy split is (first event + split.makespan_s); serve_epoch is None if the agent
    never shows the app answering. The agent may look late, so the serve epoch bounds when the app came up from above;
    it is still far tighter than the cap (the end of the agent's deploy work), which also holds its post-serve work."""
    stop = T._epoch(rows[0]) + rec["split"]["makespan_s"]
    host = re.sub(r"^https?://", "", rec.get("url") or "").split("/")[0]
    if not host:
        return stop, None
    uses = {}
    for row in rows:
        for b in T._blocks(row):
            if b.get("type") == "tool_use":
                uses[b.get("id")] = b
    for row in rows:
        if T._epoch(row) <= stop:
            continue
        for b in T._blocks(row):
            if b.get("type") != "tool_result":
                continue
            u = uses.get(b.get("tool_use_id"))
            if not u or host not in json.dumps(u.get("input")):
                continue
            # line by line: a check loop prints the stopped-site page and the app's later 200 in one output
            for line in _result_text(b).splitlines():
                if _STATUS_200.search(line) and not any(x in line for x in _NOT_THE_APP):
                    return stop, T._epoch(row)
    return stop, None


def own_stop_lead_s(rec: dict, rows: list):
    """Seconds from the agent's own last `webapp stop` command before the early stop to that stop (Part 5 section 5:
    each 403 came seconds after the agent itself had stopped its web app during a repair). None if it issued none."""
    stop = T._epoch(rows[0]) + rec["split"]["makespan_s"]
    last = None
    for row in rows:
        if T._epoch(row) > stop:
            break
        for b in T._blocks(row):
            if b.get("type") == "tool_use" and "webapp stop" in json.dumps(b.get("input")):
                last = T._epoch(row)
    return None if last is None else stop - last


def load() -> dict:
    cells = {}
    for cloud in CLOUDS:
        for key, sfx, name in TIERS:
            uuids = pt.published_uuids(cloud, key)
            recs = pt.published_records(cloud, sfx, key)
            runs = []
            for d, (u, r) in enumerate(zip(uuids, recs), 1):
                s = r["serving"]
                state = ("durable-serve" if "t1_method" in s else
                         "response-origin" if "origin" in s else "original")
                run = {"cloud": cloud, "tier": name, "key": key, "display": d, "rec": r["run"],
                       "session": u, "measured_at": r["measured_at"], "M": pt.task_M(r),
                       "state": state, "stop_code": s.get("http_code"),
                       "served_at_s": s.get("served_at_s"), "agent_finished_at_s": s.get("agent_finished_at_s"),
                       "cost_run_rate": r.get("cost_run_rate") or {}}
                if key != "easy":
                    tx = os.path.join(pt.RESULTS, cloud, f"{cloud}-{key}", "sessions", u + ".jsonl")
                    rows = T._load_rows(tx)
                    run["integrate"] = _integrate_attempts(r, rows)
                    run["cycles_lost"] = _cycles_lost(r, rows)
                    engines = set(re.findall(r"\((chromium-cli|playwright)\)", json.dumps(r["tier_run"])))
                    run["engines"] = sorted(engines)
                    users = [_text(x) for x in rows if x.get("type") == "user" and _text(x).strip()
                             and not _text(x).startswith("<task-notification")]
                    run["prompt_hostname_pin"] = "as this first app's public hostname" in users[0] or \
                        "Name the first app's public hostname" in users[0]
                    sb = [x for x in users if x.startswith("Stand up ONE second")] + \
                         [x for x in users[:1] if "Stand up ONE second" in x]
                    run["prompt_siteb_token"] = any("Name this second site so that its public hostname" in x
                                                    or "the second site's public hostname" in x for x in sb)
                if state == "durable-serve":
                    run["t1_original_s"] = _first_under_500_s(s)
                if state == "original" and s.get("http_code") not in (None, "200") and key != "easy":
                    stop, seen = observed_serve_after_stop(r, rows)
                    run["observed_missing_s"] = None if seen is None else seen - stop
                    run["own_stop_lead_s"] = own_stop_lead_s(r, rows)
                runs.append(run)
            cells[(cloud, name)] = runs
    return cells


# ------------------------------------------------------------------------------------------------
# Per-run adjustments (each returns seconds to SUBTRACT from that run's M; positive = faster)
# ------------------------------------------------------------------------------------------------


def d9b_low(run) -> float:
    """S1: the checker's own wait in each integrate attempt rejected as 'missing umami wiring'."""
    if "integrate" not in run:
        return 0.0
    return sum(a["wait_s"] for a in run["integrate"]["attempts"] if a["kind"] == "address")


def _is_address(a):
    return a["kind"] == "address"


def _is_old_engine_visit(a):
    return a["kind"] == "visit" and a["engine"] == "chromium-cli"


def _from_first(run, select, with_cycles: bool) -> float:
    """Seconds from the end of the first selected rejected attempt to the end of the integrate leg (the
    accepted attempt's last event, or the leg's last event if never accepted), plus, with_cycles, the restart
    cycles after the first that handed back integrate alone where integrate was never accepted. One span per
    run, so two selections are combined by widening the selection, never by adding two spans."""
    if "integrate" not in run:
        return 0.0
    ig = run["integrate"]
    first = [a for a in ig["attempts"] if select(a)]
    if not first:
        return 0.0
    cut = ig["leg_last"] - first[0]["turn_end"]
    if with_cycles and not ig["accepted"]:
        cut += sum(c["makespan_s"] for c in run["cycles_lost"] if c["lost"] == ["integrate"])
    return cut


def d9b_high(run) -> float:
    """S2: everything from the end of the first attempt rejected as 'missing umami wiring' until the check
    accepted (the accepted attempt's last event) or the leg ended, plus, where integrate was never accepted,
    the later restart cycles that handed back integrate alone."""
    return _from_first(run, _is_address, with_cycles=True)


def d9b_high_leg(run) -> float:
    """S2 without the restart cycles: stops at the end of the integrate leg."""
    return _from_first(run, _is_address, with_cycles=False)


def visit_low(run) -> float:
    """S9 low: the checker's wait in each attempt rejected as a lost visit read by the pre-2026-09-05 engine."""
    if "integrate" not in run:
        return 0.0
    return sum(a["wait_s"] for a in run["integrate"]["attempts"] if _is_old_engine_visit(a))


def visit_high(run) -> float:
    """S9 high: from the first lost-visit rejection read by the pre-change engine until accepted or the leg
    ended, plus restart cycles handing back integrate alone where it was never accepted."""
    return _from_first(run, _is_old_engine_visit, with_cycles=True)


def both_checker_low(run) -> float:
    """S1 + S9 low: the checker's wait in every rejection of either kind (disjoint attempts, so they add)."""
    return d9b_low(run) + visit_low(run)


def both_checker_high(run) -> float:
    """S2 + S9 high as ONE span per run: from the first rejection of either kind until accepted."""
    return _from_first(run, lambda a: _is_address(a) or _is_old_engine_visit(a), with_cycles=True)


def azure403_cap(run) -> float:
    """S3: a run whose clock the ORIGINAL predicate stopped on a non-200 is charged up to the end of the agent's
    deploy work (Part 5, section 6's cap). Returned NEGATIVE: the run gets slower."""
    if run["state"] == "original" and run["stop_code"] not in (None, "200"):
        return -max(0.0, run["agent_finished_at_s"] - run["served_at_s"])
    return 0.0


def azure403_observed(run) -> float:
    """S3obs: each run the original predicate stopped on a non-200 charged the deploy time its app was measured to
    still need, from the stop to the agent's own first sight of the app answering (observed_serve_after_stop); a run
    with no such sight falls back to the cap. Returned NEGATIVE: the run gets slower."""
    if run["state"] == "original" and run["stop_code"] not in (None, "200"):
        seen = run.get("observed_missing_s")
        return -seen if seen is not None else azure403_cap(run)
    return 0.0


def original_predicate(run) -> float:
    """S7: a durable-serve run re-timed by the original predicate (first response < 500) from its own poll
    evidence. Positive = faster. Reintroduces the edge-answer stops the 2026-09-04 amendment removed."""
    if run["state"] != "durable-serve" or run.get("t1_original_s") is None:
        return 0.0
    return max(0.0, run["served_at_s"] - run["t1_original_s"])


# ------------------------------------------------------------------------------------------------
# Verdicts
# ------------------------------------------------------------------------------------------------

COMPARISONS = []
for _key, _sfx, _name in TIERS:
    for _a, _b in combinations(CLOUDS, 2):
        COMPARISONS.append((f"{_name}: {pt.ANON[_a]} vs {pt.ANON[_b]}", (_a, _name), (_b, _name)))
for _c in CLOUDS:
    COMPARISONS.append((f"{pt.ANON[_c]}: Medium online vs disclosed", (_c, ONLINE), (_c, DISCL)))


def _decide(x, y):
    lo, hi = rt.boot_diff_ci(x, y)
    excl = lo > 0 or hi < 0
    _, p, _ = rt.mannwhitney(x, y)
    if excl and p < ALPHA:
        verdict = "A>B" if lo > 0 else "A<B"
    elif not excl and p >= ALPHA:
        verdict = "undecided"
    else:
        verdict = "blocked"
    return {"ci": (round(lo), round(hi)), "p": p, "verdict": verdict}


def halves_test(ms_in_order):
    n = len(ms_in_order)
    h = n // 2
    a, b = ms_in_order[:h], ms_in_order[n - h:]
    _, p, method = rt.mannwhitney(a, b)
    return {"p": p, "first_mean": sum(a) / len(a), "second_mean": sum(b) / len(b), "method": method}


def _suite(cell_ms):
    """Part 4's suite bootstrap (paper_tables._suite_replicates via suite_uncertainty), on adjusted cells.
    Easy cells are never adjusted by any scenario, so the paired Easy-cell cost draw stays run-for-run and
    paper_tables' own pairing guard runs unchanged."""
    cells = []
    for cloud in CLOUDS:
        for key, sfx, name in TIERS:
            recs = pt.published_records(cloud, sfx, key) if name == EASY else None
            cells.append({"cloud": cloud, "anon": pt.ANON[cloud], "tier": name,
                          "Ms": list(cell_ms[(cloud, name)]),
                          "cost": [(r.get("cost_run_rate") or {}) for r in recs] if recs else []})
    su = pt.suite_uncertainty(cells)
    means = {c: {t: sum(cell_ms[(c, t)]) / len(cell_ms[(c, t)]) for _, _, t in TIERS} for c in CLOUDS}
    ex = {c: weighting.suite_total(means[c]) for c in CLOUDS}
    gx = {c: weighting.geomean_ratio(means[c], means[pt.GX_NORMALIZER]) for c in CLOUDS}
    agree = sorted(ex, key=ex.get) == sorted(gx, key=gx.get)
    cost = {}
    for c in cells:
        if c["tier"] == EASY:
            rates = [r for r in (pt._hourly(x) for x in c["cost"]) if r is not None]
            cost[c["cloud"]] = sum(rates) / len(rates)
    runs = [weighting.Run(label=c, time=ex[c], cost=cost[c]) for c in CLOUDS]
    front = {r.label for r in weighting.pareto_frontier(runs)}
    loo = {}
    for a, b in combinations(CLOUDS, 2):
        r = weighting.leave_one_out(means[a], means[b])
        loo[f"{pt.ANON[a]}-{pt.ANON[b]}"] = r["flips_on"]
    pairs = {}
    for (a, b), v in su["_pairs"].items():
        pairs[f"{pt.ANON[a]}-{pt.ANON[b]}"] = {"ci": (round(v["ci"][0]), round(v["ci"][1])),
                                                 "decided": v["decided"]}
    return {"E_X": {pt.ANON[c]: round(ex[c]) for c in CLOUDS},
            "G_X": {pt.ANON[c]: round(gx[c], 3) for c in CLOUDS},
            "E_X_ci": {pt.ANON[c]: tuple(round(x) for x in su[c]["ex_ci"]) for c in CLOUDS},
            "G_X_ci": {pt.ANON[c]: tuple(round(x, 3) for x in su[c]["gx_ci"]) for c in CLOUDS},
            "pairs": pairs, "agree": agree,
            "E_order": [pt.ANON[c] for c in sorted(ex, key=ex.get)],
            "G_order": [pt.ANON[c] for c in sorted(gx, key=gx.get)],
            "frontier": {pt.ANON[c]: (c in front) for c in CLOUDS},
            "front_freq": {pt.ANON[c]: su[c]["front_pct"] for c in CLOUDS},
            "loo_flips": loo}


def _hourly_at(crr: dict, level: str):
    """paper_tables._hourly generalised to the three disclosed usage levels (low = 10^4, medium = 5x10^5,
    high = 10^7 requests a month); a standing surface carries one rate at every level."""
    if crr.get("kind") == "standing" and crr.get("monthly_usd") is not None:
        return crr["monthly_usd"] / 730.0
    te = crr.get("traffic_estimate") or {}
    return te[level] / 730.0 if te.get(level) is not None else None


def frontier_six(kept: dict, ex: dict) -> dict:
    """Membership at each of the six representative points Table 5.2's note discloses: cost coordinate from
    the Easy cell or from all three cells, read at each usage level, against the scenario's E_X."""
    out = {}
    for basis in ("Easy cell", "all cells"):
        for level in ("low", "medium", "high"):
            cost = {}
            for c in CLOUDS:
                rs = [r for (cc, t), runs in kept.items() if cc == c and (basis == "all cells" or t == EASY)
                      for r in runs]
                v = [x for x in (_hourly_at(r["cost_run_rate"], level) for r in rs) if x is not None]
                cost[c] = sum(v) / len(v)
            front = {r.label for r in weighting.pareto_frontier(
                [weighting.Run(label=c, time=ex[c], cost=cost[c]) for c in CLOUDS])}
            out[f"{basis}, {level}"] = {pt.ANON[c]: (c in front) for c in CLOUDS}
    return out


def evaluate(cells_runs: dict, adjust=None, keep=None) -> dict:
    """adjust(run) -> seconds to subtract from M; keep(run) -> bool. Returns every verdict."""
    cell_ms, order, ns, kept_all = {}, {}, {}, {}
    for k, runs in cells_runs.items():
        kept = [r for r in runs if (keep is None or keep(r))]
        kept_all[k] = kept
        ms = [r["M"] - (adjust(r) if adjust else 0.0) for r in kept]
        cell_ms[k] = ms
        ns[k] = len(ms)
        order[k] = [m for _, m in sorted(zip([r["measured_at"] for r in kept], ms))]
    est = {k: n >= MIN_N for k, n in ns.items()}
    out = {"n": {f"{pt.ANON[k[0]]} {k[1]}": ns[k] for k in cells_runs},
           "mean": {f"{pt.ANON[k[0]]} {k[1]}": (round(sum(v) / len(v), 1) if v else None)
                    for k, v in cell_ms.items()},
           "comparisons": {}, "halves": {}}
    for label, a, b in COMPARISONS:
        if est[a] and est[b]:
            out["comparisons"][label] = _decide(cell_ms[a], cell_ms[b])
        else:
            out["comparisons"][label] = {"verdict": "not estimable", "n": (ns[a], ns[b])}
    for k, v in order.items():
        if len(v) >= 4:
            out["halves"][f"{pt.ANON[k[0]]} {k[1]}"] = halves_test(v)
    out["suite"] = _suite(cell_ms) if all(est.values()) else None
    if out["suite"] is not None:
        ex = {c: sum(sum(cell_ms[(c, t)]) / len(cell_ms[(c, t)]) for _, _, t in TIERS) for c in CLOUDS}
        out["suite"]["frontier_six"] = frontier_six(kept_all, ex)
    return out


# ------------------------------------------------------------------------------------------------
# Scenarios
# ------------------------------------------------------------------------------------------------

def _in(cloud, tier):
    return lambda r: r["cloud"] == cloud and r["tier"] == tier


def build_scenarios(cells_runs):
    gcp_lo = "2026-08-29"
    gcp_hi_day = "2026-09-02T23:59:59.999999+00:00"
    gcp_runs = [r for (c, _), rs in cells_runs.items() if c == "gcp" for r in rs]
    gcp_first = min(r["measured_at"] for r in gcp_runs)
    gcp_last = max(r["measured_at"] for r in gcp_runs)
    aws_b_after = lambda r: "playwright" in r.get("engines", [])          # noqa: E731
    aws_b_before = lambda r: "chromium-cli" in r.get("engines", [])       # noqa: E731

    def only(cloud, tier, pred):
        return lambda r: (not (r["cloud"] == cloud and r["tier"] == tier)) or pred(r)

    first_form = lambda r: not r.get("prompt_hostname_pin") and not r.get("prompt_siteb_token")  # noqa: E731
    in_d9b = lambda r: r["cloud"] in ("aws", "gcp") and r["tier"] == ONLINE   # noqa: E731

    def add(*fs):
        return lambda r: sum(f(r) for f in fs)

    def both(*ks):
        return lambda r: all(k(r) for k in ks)

    S = {}
    S["P"] = ("published", None, None)
    S["S1"] = ("D9b low: minus the checker's own wait in each integrate attempt rejected as 'missing umami "
               "wiring' (AWS and GCP Medium online)", d9b_low, None)
    S["S2"] = ("D9b high: minus everything from the end of the first such attempt until the check accepted "
               "or the leg ended, plus the later restart cycles that handed back integrate alone where it "
               "was never accepted", d9b_high, None)
    S["S2leg"] = ("D9b high, stopping at the end of the integrate leg (no restart cycles)", d9b_high_leg, None)
    S["S2x"] = ("D9b box corner adverse to AWS: AWS online at the high count, GCP online at the low count",
                lambda r: d9b_high(r) if r["cloud"] == "aws" else d9b_low(r), None)
    S["S2y"] = ("D9b box corner adverse to GCP: GCP online at the high count, AWS online at the low count",
                lambda r: d9b_high(r) if r["cloud"] == "gcp" else d9b_low(r), None)
    S["S3"] = ("Azure 403 cap: the five Azure runs the original predicate stopped on an edge 403 charged up "
               "to the end of the agent's deploy work (Part 5 section 6)", azure403_cap, None)
    S["S3a"] = ("Azure 403 cap box corner: only the one Azure Medium (online) run capped",
                lambda r: azure403_cap(r) if r["tier"] == ONLINE else 0.0, None)
    S["S3b"] = ("Azure 403 cap box corner: only the four Azure Medium (disclosed) runs capped",
                lambda r: azure403_cap(r) if r["tier"] == DISCL else 0.0, None)
    S["S3obs"] = ("Azure 403 runs charged the deploy time their app was measured to still need: from the stop to the "
                  "agent's own first sight of the app answering (the cap's upper bound also holds post-serve work)",
                  azure403_observed, None)
    S["S11lowobs"] = ("S1 + S3obs + S9: the rework and the visit change at their low counts with the measured 403 time",
                      add(both_checker_low, azure403_observed), None)
    S["S3x"] = ("Azure 403 runs excluded (Part 5 section 3's own sensitivity)", None,
                lambda r: azure403_cap(r) == 0.0)
    S["S4after"] = ("AWS Medium (disclosed) restricted to its runs read by the post-2026-09-05 engine",
                    None, only("aws", DISCL, aws_b_after))
    S["S4before"] = ("AWS Medium (disclosed) restricted to its runs read by the pre-2026-09-05 engine",
                     None, only("aws", DISCL, aws_b_before))
    S["S4aws-online-before"] = ("AWS Medium (online), the other cell failing the halves test, restricted to its "
                                "runs before the 2026-09-01 address-rule and prompt change (the side with n >= 5)",
                                None, only("aws", ONLINE, first_form))
    S["S4aws-online-after"] = ("AWS Medium (online) restricted to its runs after that change (n = 3)",
                               None, only("aws", ONLINE, lambda r: not first_form(r)))
    S["S5"] = ("common window: every cell restricted to runs measured on 2026-08-29 to 2026-09-02 (UTC "
               "calendar days)", None, lambda r: gcp_lo <= r["measured_at"] <= gcp_hi_day)
    S["S5exact"] = ("common window at GCP's exact span (first to last GCP run)", None,
                    lambda r: gcp_first <= r["measured_at"] <= gcp_last)
    S["S6"] = ("combined: S2 + S3 + S4after", add(d9b_high, azure403_cap), only("aws", DISCL, aws_b_after))
    S["S6low"] = ("combined at the low D9b count: S1 + S3 + S4after", add(d9b_low, azure403_cap),
                  only("aws", DISCL, aws_b_after))
    S["S6b"] = ("combined with the disclosed-only cap corner: S2 + S3b + S4after",
                add(d9b_high, lambda r: azure403_cap(r) if r["tier"] == DISCL else 0.0),
                only("aws", DISCL, aws_b_after))
    S["S7"] = ("original predicate on every run: the durable-serve runs re-timed at their first response "
               "< 500 (reintroduces the edge-answer stops the 2026-09-04 amendment removed)",
               original_predicate, None)
    S["S8"] = ("one prompt and check form per cell: Medium online restricted to the 25 runs given neither "
               "the 2026-09-01 hostname pin nor the second-site token request", None,
               lambda r: r["tier"] != ONLINE or first_form(r))
    S["S9"] = ("2026-09-05 visit change, low: minus the checker's wait in each attempt rejected as a lost "
               "visit read by the pre-change engine", visit_low, None)
    S["S9high"] = ("2026-09-05 visit change, high: minus everything from the first such rejection until "
                   "accepted, plus restart cycles handing back integrate alone", visit_high, None)
    S["S10low"] = ("S1 + S9: both integrate-checker changes at their low counts", both_checker_low, None)
    S["S10"] = ("S2 + S9high: both integrate-checker changes at their high counts (one span per run)",
                both_checker_high, None)
    S["S11low"] = ("S1 + S3 + S9: every records-sized adjustment at its low count, no run dropped",
                   add(both_checker_low, azure403_cap), None)
    S["S11"] = ("S2 + S3 + S9high: every records-sized adjustment at its high count, no run dropped",
                add(both_checker_high, azure403_cap), None)
    return S


# ------------------------------------------------------------------------------------------------
# Verdict table
# ------------------------------------------------------------------------------------------------

# Run and reported, but not counted toward HOLDS: S7 re-times the durable-serve runs at the first response
# below 500, and on every run it moves that response is one the record itself identifies as the platform edge
# rather than the app (origin[].edge, e.g. 'aws-alb-no-upstream' with the body '404 No Such Service'). It is
# the clock stop the 2026-09-04 amendment exists to remove, so it is not an adjustment FOR that change.
NOT_COUNTED = {"S7"}

# What the paper (Part 5, section 5, "Which verdicts hold across the instrument's changes") counts: the split of
# AWS Medium (disclosed) at the 2026-09-05 change, and the five Azure runs the original predicate stopped early
# charged the deploy time their app was measured to still need (S3obs). The cap (S3) is the upper bound and is
# reported, not counted: it also charges the agent's post-serve work. Leaving the five out (S3x) is not a correction.
# The integration check's rejections (S1, S2) are part of the task and SIZED rather than counted; S5 and S8 restrict
# by date or by prompt form, which the paper's dating to the ten days measured covers. The full table above keeps
# every scenario, so a reader can count differently.
PAPER_COUNTED = {"S4after", "S4before", "S3obs"}


def verdict_rows(results):
    pub = results["P"]
    rows = []
    for label, _, _ in COMPARISONS:
        pv = pub["comparisons"][label]["verdict"]
        per, breaks = {}, []
        for sid, res in results.items():
            v = res["comparisons"][label]["verdict"]
            per[sid] = v
            if v != "not estimable" and v != pv and sid not in NOT_COUNTED:
                breaks.append(sid)
        rows.append({"verdict": label, "published": pv, "by": per, "breaks": breaks})
    def _dir(ci):
        return "A>B" if ci[0] > 0 else ("A<B" if ci[1] < 0 else "undecided")

    for pair in ("AWS-GCP", "AWS-Azure", "Azure-GCP"):
        pv = _dir(pub["suite"]["pairs"][pair]["ci"])
        per, breaks = {}, []
        for sid, res in results.items():
            if res["suite"] is None:
                per[sid] = "not estimable"
                continue
            v = _dir(res["suite"]["pairs"][pair]["ci"])
            per[sid] = f"{v} {res['suite']['pairs'][pair]['ci']}"
            if v != pv and sid not in NOT_COUNTED:
                breaks.append(sid)
        rows.append({"verdict": f"suite E_X {pair} (interval half)", "published": pv, "by": per,
                     "breaks": breaks})
    for cl in ("GCP", "Azure"):
        def gdir(ci):
            return "below AWS" if ci[1] < 1.0 else ("above AWS" if ci[0] > 1.0 else "undecided")
        pv = gdir(pub["suite"]["G_X_ci"][cl])
        per, breaks = {}, []
        for sid, res in results.items():
            if res["suite"] is None:
                per[sid] = "not estimable"
                continue
            v = gdir(res["suite"]["G_X_ci"][cl])
            per[sid] = f"{v} {res['suite']['G_X_ci'][cl]}"
            if v != pv and sid not in NOT_COUNTED:
                breaks.append(sid)
        rows.append({"verdict": f"suite G_X {cl} vs AWS (interval half)", "published": pv, "by": per,
                     "breaks": breaks})
    for name, getter in (("agree?", lambda s: s["agree"]),
                         ("E_X point order", lambda s: tuple(s["E_order"])),
                         ("frontier AWS", lambda s: s["frontier"]["AWS"]),
                         ("frontier GCP", lambda s: s["frontier"]["GCP"]),
                         ("frontier Azure", lambda s: s["frontier"]["Azure"]),
                         ("frontier, all six representative points (AWS no, GCP yes, Azure yes)",
                          lambda s: all(v == {"AWS": False, "GCP": True, "Azure": True}
                                        for v in s["frontier_six"].values()))):
        pv = getter(pub["suite"])
        per, breaks = {}, []
        for sid, res in results.items():
            if res["suite"] is None:
                per[sid] = "not estimable"
                continue
            v = getter(res["suite"])
            per[sid] = v
            if v != pv and sid not in NOT_COUNTED:
                breaks.append(sid)
        rows.append({"verdict": name, "published": pv, "by": per, "breaks": breaks})
    return rows


# ------------------------------------------------------------------------------------------------
# Calibration: the published figures, reproduced by the released tools AND by code written here
# ------------------------------------------------------------------------------------------------

def _exact_mw_p_by_enumeration(x, y):
    """Two-sided exact rank-test p by enumerating every split of the pooled ranks (independent of
    rank_test's recurrence). Midranks for ties; p = P(|U - mn/2| >= observed) under all C(m+n, m) splits."""
    from itertools import combinations as comb
    pooled = list(x) + list(y)
    m, n = len(x), len(y)
    order = sorted(range(m + n), key=lambda i: pooled[i])
    ranks = [0.0] * (m + n)
    i = 0
    while i < m + n:
        j = i
        while j + 1 < m + n and pooled[order[j + 1]] == pooled[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    obs = abs(sum(ranks[:m]) - m * (m + n + 1) / 2.0)
    hit = tot = 0
    for idx in comb(range(m + n), m):
        tot += 1
        if abs(sum(ranks[i] for i in idx) - m * (m + n + 1) / 2.0) >= obs - 1e-9:
            hit += 1
    return hit / tot


def _own_boot_diff(x, y, seed, B=10000):
    """Percentile bootstrap on a difference of means, written here without rank_test, same seed and draws."""
    import random as _r
    g = _r.Random(seed)
    d = sorted(sum(g.choices(x, k=len(x))) / len(x) - sum(g.choices(y, k=len(y))) / len(y) for _ in range(B))
    return d[int(0.025 * B)], d[int(0.975 * B) - 1]


def calibrate(cells_runs):
    out = {}
    # Table 5.1 M: released paper_tables cell() vs the records summed here
    t51 = {}
    for cloud in CLOUDS:
        for key, sfx, name in TIERS:
            c = pt.cell(cloud, sfx, key, name)
            own = [sum((l or {}).get("makespan_s") or 0.0 for l in
                       [r.get("split")] + [o.get("split") for o in r["tier_run"]["operations"]] +
                       [cy.get("split") for cy in ((r["tier_run"].get("durability") or {}).get("cycles") or [])])
                   if r.get("tier_run") else (r.get("split") or {}).get("makespan_s")
                   for r in pt.published_records(cloud, sfx, key)]
            t51[f"{pt.ANON[cloud]} {name}"] = {"n": c["n"], "M_released": round(c["M_mean"], 1),
                                                "M_own": round(sum(own) / len(own), 1),
                                                "ci": (round(c["M_lo"]), round(c["M_hi"]))}
    out["table51"] = t51
    # Section 5's twelve comparisons: rank_test vs own bootstrap and enumeration
    rows = {}
    for label, a, b in COMPARISONS:
        x = [r["M"] for r in cells_runs[a]]
        y = [r["M"] for r in cells_runs[b]]
        lo, hi = rt.boot_diff_ci(x, y)
        olo, ohi = _own_boot_diff(x, y, rt.BOOT_SEED)
        _, p, method = rt.mannwhitney(x, y)
        rows[label] = {"ci_released": (round(lo), round(hi)), "ci_own": (round(olo), round(ohi)),
                       "p_released": round(p, 4), "method": method,
                       "p_enumerated": round(_exact_mw_p_by_enumeration(x, y), 4)}
    out["twelve"] = rows
    # exchangeability halves, released mannwhitney vs enumeration
    hv = {}
    for k, runs in cells_runs.items():
        v = [r["M"] for r in sorted(runs, key=lambda r: r["measured_at"])]
        h = len(v) // 2
        _, p, _ = rt.mannwhitney(v[:h], v[len(v) - h:])
        hv[f"{pt.ANON[k[0]]} {k[1]}"] = {"p_released": round(p, 4),
                                          "p_enumerated": round(_exact_mw_p_by_enumeration(v[:h], v[len(v) - h:]), 4),
                                          "first": round(sum(v[:h]) / h), "second": round(sum(v[len(v) - h:]) / h)}
    out["halves"] = hv
    # Table 5.2 and its note: the released suite bootstrap and the enumerated frontier figure
    cells = pt.all_cells()
    su = pt.suite_uncertainty(cells)
    fe = pt.frontier_exact(cells)
    out["table52_rows"] = pt.table_52(cells).splitlines()
    out["suite"] = {"E_X_ci": {pt.ANON[c]: tuple(round(x) for x in su[c]["ex_ci"]) for c in CLOUDS},
                    "G_X_ci": {pt.ANON[c]: tuple(round(x, 3) for x in su[c]["gx_ci"]) for c in CLOUDS},
                    "pairs": {f"{pt.ANON[a]}-{pt.ANON[b]}": tuple(round(x) for x in v["ci"])
                              for (a, b), v in su["_pairs"].items()},
                    "front_pct_paired": {pt.ANON[c]: su[c]["front_pct"] for c in CLOUDS},
                    "front_pct_unpaired": {pt.ANON[c]: su[c]["front_pct_unpaired"] for c in CLOUDS},
                    "azure_front_enumerated_pct": round(fe["pct"], 6)}
    return out


def seed_spread(cells_runs, scen, items, seeds=range(200)):
    """For the verdicts a counted scenario breaks: at how many of 200 bootstrap seeds the interval half
    still excludes zero (the rank test does not depend on the seed)."""
    res = {}
    for sid, label in items:
        desc, adj, keep = scen[sid]
        cms = {}
        for k, runs in cells_runs.items():
            kept = [r for r in runs if keep is None or keep(r)]
            cms[k] = [r["M"] - (adj(r) if adj else 0.0) for r in kept]
        _, a, b = next(c for c in COMPARISONS if c[0] == label)
        x, y = cms[a], cms[b]
        excl = 0
        for sd in seeds:
            lo, hi = rt.boot_diff_ci(x, y, seed=sd)
            excl += (lo > 0 or hi < 0)
        _, p, _ = rt.mannwhitney(x, y)
        res[f"{sid} | {label}"] = {"seeds_excluding_zero": excl, "of": len(list(seeds)), "rank_p": round(p, 4)}
    return res


def main():
    cells_runs = load()
    scen = build_scenarios(cells_runs)
    results = {}
    for sid, (desc, adj, keep) in scen.items():
        results[sid] = evaluate(cells_runs, adj, keep)
        results[sid]["definition"] = desc
    rows = verdict_rows(results)
    cal = calibrate(cells_runs)
    breaks = [(sid, r["verdict"]) for r in rows if ":" in r["verdict"] for sid in r["breaks"]]
    spread = seed_spread(cells_runs, scen, breaks)

    for sid, res in results.items():
        print(f"\n== {sid}: {res['definition']}")
        print("   n/mean: " + "; ".join(f"{k} {res['n'][k]}/{res['mean'][k]}" for k in res["n"]))
        print("   halves: " + "; ".join(f"{k} p={v['p']:.4f}" for k, v in res["halves"].items()))
        for label, c in res["comparisons"].items():
            if c["verdict"] != "not estimable":
                print(f"   {label:40s} ci={c['ci']} p={c['p']:.4f} {c['verdict']}")
        if res["suite"]:
            s = res["suite"]
            print(f"   E_X {s['E_X']}  G_X {s['G_X']}  agree={s['agree']}  frontier={s['frontier']}  "
                  f"pairs={ {k: v['ci'] for k, v in s['pairs'].items()} }")
    print("\n== CALIBRATION")
    print(json.dumps(cal, indent=1, default=str))
    print("\n== SEED SPREAD of the comparisons a counted scenario breaks (interval half, 200 seeds)")
    for k, v in spread.items():
        print(f"   {k:70s} {v['seeds_excluding_zero']}/{v['of']} rank p={v['rank_p']}")
    print("\n== VERDICTS")
    for r in rows:
        s7 = r["by"].get("S7")
        pb = [b for b in r["breaks"] if b in PAPER_COUNTED]
        paper = "paper: HOLDS" if not pb else "paper: NOT SETTLED (" + ", ".join(pb) + ")"
        print(f"{r['verdict']:42s} published={r['published']!s:12s} {paper:34s} all scenarios: "
              f"{'HOLDS' if not r['breaks'] else 'DOES_NOT_HOLD (' + ', '.join(r['breaks']) + ')'}"
              f"   [S7, not counted: {s7}]")
    if "--json" in sys.argv:
        path = sys.argv[sys.argv.index("--json") + 1]
        per_run = {f"{pt.ANON[k[0]]} {k[1]} d{r['display']}": {
            "session": r["session"], "measured_at": r["measured_at"], "M": r["M"], "state": r["state"],
            "engines": r.get("engines"), "hostname_pin": r.get("prompt_hostname_pin"),
            "siteb_token": r.get("prompt_siteb_token"),
            "d9b_low": d9b_low(r), "d9b_high": d9b_high(r), "d9b_high_leg": d9b_high_leg(r),
            "visit_low": visit_low(r), "visit_high": visit_high(r), "azure403_cap": -azure403_cap(r),
            "original_predicate": original_predicate(r),
            "observed_missing_s": r.get("observed_missing_s"), "own_stop_lead_s": r.get("own_stop_lead_s"),
            "azure403_observed": -azure403_observed(r),
            "attempts": (r.get("integrate") or {}).get("attempts"),
            "cycles_lost": r.get("cycles_lost")} for k, rs in cells_runs.items() for r in rs}
        json.dump({"calibration": cal, "seed_spread": spread, "results": results, "verdicts": rows,
                   "per_run": per_run}, open(path, "w"),
                  indent=1, default=str)
        print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
