"""Recompute EVERY published table field from its run record and diff against the README.

Written 2026-09-07 after a pricing bug reached the published tables. The point is that a table cell is
a claim, and a claim that can be rechecked mechanically should be, not read over. Run it after any
ingest, re-price or table edit:

    python3 audit_fields.py            # exit 1 if anything disagrees

The derivations here are the AUTHORITY, taken from build_tables.py for the easy tier and from the
PLAYBOOK for the medium tier. If a column legitimately changes definition, change it HERE in the same
commit, so the two can never drift apart silently again.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

import paper_tables as pt
from build_tables import run_cost

CELLS = [(c, sfx, key) for c in ("aws", "gcp", "azure", "redu")
         for key, sfx in (("easy", ""), ("medium-a", "-medium-a"), ("medium-b", "-medium-b"))]

# `total` rounding differs by cloud and each cell is internally consistent: the
# public clouds sum the ROUNDED columns, redu rounds the SUM. Both are accepted, neither is assumed.
TOTAL_CONVENTIONS = ("sum-of-rounded", "round-of-sum")


def _g(d, *ks):
    for k in ks:
        d = (d or {}).get(k) if isinstance(d, dict) else None
    return d


def expected_easy(rec: dict) -> dict:
    sp = rec.get("split") or {}
    crr = rec.get("cost_run_rate") or {}
    te = crr.get("traffic_estimate") or {}
    return {"t1 (s)": rec.get("time_to_serving_s"),
            "platform (s)": sp.get("critical_platform_s"),
            "agent (s)": sp.get("critical_agent_s"),
            "steps": rec.get("steps"),
            "tokens": _g(rec, "deploy", "tokens", "output_tokens"),
            "agent $": run_cost(rec),
            "fixed $/mo": crr.get("monthly_usd"),
            "$/mo @ 10k req": te.get("low"), "$/mo @ 500k req": te.get("medium"),
            "$/mo @ 10M req": te.get("high")}


def expected_medium(rec: dict) -> dict:
    tr = rec.get("tier_run") or {}
    ops = tr.get("operations") or []
    by = {o["op_id"]: o for o in ops}
    cyc = (tr.get("durability") or {}).get("cycles") or []
    mk = lambda k: (_g(by.get(k), "split", "makespan_s"))          # noqa: E731
    crr = rec.get("cost_run_rate") or {}
    te = crr.get("traffic_estimate") or {}
    sc = tr.get("score") or {}
    # agent $ = the task being measured: deploy round + operations + durability. Teardown is harness
    # bookkeeping and stays out (see PLAYBOOK). Parts are disjoint windows of one session.
    agent = (sum((r.get("cost") or 0.0) for r in rec.get("rounds") or [])
             + sum((o.get("agent_cost_usd") or 0.0) for o in ops if o["op_id"] != "deploy-serve")
             + sum((c.get("agent_cost_usd") or 0.0) for c in cyc))
    return {"deploy (t1)": rec.get("time_to_serving_s"),
            "register": mk("mutate:register"), "site-b": mk("deploy-site-b"),
            "integrate": mk("integrate"),
            "durability": sum(_g(c, "split", "makespan_s") or 0.0 for c in cyc),
            "agent $": agent, "fixed $/mo": crr.get("monthly_usd"),
            "$/mo @ 10k": te.get("low"), "$/mo @ 500k": te.get("medium"), "$/mo @ 10M": te.get("high"),
            "tier": f"{sc.get('passed')}/{sc.get('total')}"}


def published_rows(cloud: str, cell: str) -> list:
    lines = [l for l in open(f"results/{cloud}/{cloud}-{cell}/README.md") if l.startswith("|")]
    hdr = [h.strip() for h in lines[0].strip().strip("|").split("|")]
    out = []
    for l in lines[2:]:
        c = [x.strip() for x in l.strip().strip("|").split("|")]
        if len(c) == len(hdr):
            out.append(dict(zip(hdr, c)))
    return out


def _num(x):
    if x in (None, "-", ""):
        return None
    return float(str(x).replace("$", "").replace(",", ""))


def main() -> int:
    bad = checked = 0
    for cloud, sfx, cell in CELLS:
        recs = pt.published_records(cloud, sfx, cell)
        rows = published_rows(cloud, cell)
        if len(recs) != len(rows):
            print(f"  ROW COUNT {cloud}-{cell}: {len(recs)} records vs {len(rows)} published rows")
            bad += 1
            continue
        for i, (rec, row) in enumerate(zip(recs, rows), 1):
            exp = expected_easy(rec) if cell == "easy" else expected_medium(rec)
            for col, ev in exp.items():
                if col not in row:
                    continue
                checked += 1
                if col == "tier":
                    if row[col] != ev:
                        print(f"  {cloud}-{cell} row{i} {col}: published {row[col]!r} expected {ev!r}")
                        bad += 1
                    continue
                p = _num(row[col])
                if ev is None or p is None:
                    if (ev is None) != (p is None):
                        print(f"  {cloud}-{cell} row{i} {col}: published {row[col]!r} expected {ev!r}")
                        bad += 1
                    continue
                # Compare against the RAW value within half a unit, not against a re-rounded one: a
                # cell is the raw number rendered to cents, so an exact tie (azure-easy row5 is
                # $2.685) is correct whichever way the renderer broke it. Re-rounding here would
                # report a half-cent tie-break as a data defect.
                tol = 0.005 + 1e-9 if "$" in col else 0.5 + 1e-9
                if abs(p - ev) > tol:
                    print(f"  {cloud}-{cell} row{i} {col}: published {p} expected {ev}")
                    bad += 1
            if cell != "easy" and "total" in row:
                checked += 1
                raw = [rec.get("time_to_serving_s") or 0.0]
                raw += [exp[k] or 0.0 for k in ("register", "site-b", "integrate", "durability")]
                t = _num(row["total"])
                if not any(abs(v - t) < 0.51 for v in (sum(round(x) for x in raw), round(sum(raw)))):
                    print(f"  {cloud}-{cell} row{i} total: published {t}, neither "
                          f"sum-of-rounded {sum(round(x) for x in raw)} nor round-of-sum {round(sum(raw))}")
                    bad += 1
    print(f"\nchecked {checked} published field values across {len(CELLS)} cells: "
          f"{bad if bad else 'NO'} mismatch{'es' if bad != 1 else ''}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
