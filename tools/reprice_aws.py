"""Re-price already-recorded AWS runs with the CloudTrail mechanism, without re-running them.

Why this exists. The published AWS rows were priced by two different mechanisms. The older one (Price
List Query API over a Resource-Explorer inventory) excluded public IPv4 addresses and counted one
container of N, so its figures are a silent under-charge; the newer one (CloudTrail discovery + the
published bulk offer index) prices both. Mixing them inside one cost column makes a cell's costs
non-comparable, and the Part 4 frontier reads that column.

Nothing here re-runs a benchmark. Discovery reads CloudTrail management events, which AWS retains for
90 days, and pricing reads the published offer index. Both are read-only and free.

The one difference from the live path: at cost-snapshot time the resources were still running, so
``run_rate_universal`` could ask them for SKU selectors (``describe_live``). Re-pricing happens after
teardown, so there is nothing to ask; selectors must come from the create event itself. Whether that
loses anything is not assumed, it is MEASURED: ``--control`` re-prices the runs that were already
priced by the live path and diffs against their published figure. Trust the retroactive numbers only
for runs whose architecture the control reproduces.

Usage:
    python3 reprice_aws.py --control                    # reproduce known runs, report the diff
    python3 reprice_aws.py --cell aws-medium-a          # re-price one cell's published runs
    python3 reprice_aws.py --cell aws-medium-a --apply  # ... and write results/<cell>/REPRICE.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from acspeed.adapters import aws_ct_cost as ct
from acspeed.cost import RateComponent, compose_run_rate

STAGING = os.environ.get("ACSPEED_STAGING") or os.path.join(
    os.environ.get("ACSPEED_DATA") or os.path.expanduser("~/.acspeed"), "_staging")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The window is scoped by the run token, so it only has to be generous enough to contain the run's
# creates. The live path looks back 8h from the snapshot; measured_at IS that snapshot.
LOOKBACK_H = 8
LOOKAHEAD_MIN = 15


def window(rec: dict):
    ma = dt.datetime.fromisoformat(rec["measured_at"])
    start = (ma - dt.timedelta(hours=LOOKBACK_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (ma + dt.timedelta(minutes=LOOKAHEAD_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, end


def reprice(rec: dict, regions, profile=None) -> dict:
    """The pricing half of run_rate_universal, over an ABSOLUTE window and with no live describe.

    Deliberately mirrors run_rate_universal rather than calling it: that function derives its window
    from the wall clock, which is exactly what a retroactive re-price cannot use.
    """
    token = rec.get("run_token")
    if not token:
        return {"ok": False, "error": "no run token to scope by"}
    start, end = window(rec)
    resources, unclassified = ct.discover(start, end, token, regions, profile)
    resources, td_notes = ct.collapse_task_definitions(resources)

    comps, unpriced, no_sku, detail = [], list(unclassified), [], []
    for r in resources:
        for part in ct.billable_parts(r):
            qty = float(part.get("quantity") or 1.0) * float(r.get("runner_count") or 1)
            # describe=None everywhere: the resource is gone, so every selector must come from the
            # create event. A vendor whose create vocabulary the price list does not share will land
            # in `unpriced` rather than be guessed at.
            p = ct.price_resource_universal(part, profile=profile, describe=None)
            if p.get("priced"):
                comps.append(RateComponent(name=f"{part['kind']}", hourly_usd=p["hourly_usd"] * qty,
                                           raw_unit_price=p["raw"], native_unit=p["unit"],
                                           quantity=qty))
                detail.append({"kind": part["kind"], "part": part.get("part"),
                               "region": part["region"], "quantity": qty,
                               "usagetype": p["usagetype"], "sku": p["sku"]})
            else:
                reason = str(p.get("reason") or "")
                if reason.startswith("ambiguous") and p.get("from_own_service"):
                    unpriced.append(f"{part['kind']}[{part.get('part')}]@{part['region']}: {reason}")
                else:
                    no_sku.append(f"{part['src']}:{part['event'] or part['kind']}@{part['region']}")
    if not comps and not resources:
        return {"ok": False, "error": f"no billable resource recorded for token {token}",
                "window": [start, end], "unpriced_resources": unpriced}
    rr = compose_run_rate(
        comps, provider="aws",
        region=(resources[0]["region"] if resources else "?"),
        flavor="+".join(sorted({r["kind"] for r in resources})) or "?",
        capture_date=rec["measured_at"][:10],
        price_source="AWS published price list (bulk offer index), on-demand per C19; resources "
                     "discovered from CloudTrail management events; re-priced retroactively, no live "
                     "describe",
    )
    d = rr.to_dict()
    d.update({"discovery": "cloudtrail", "priced_resources": detail,
              "unpriced_resources": unpriced, "no_sku_match": sorted(set(no_sku)),
              "ok": not unpriced, "window": [start, end], "n_resources": len(resources),
              "task_definition_collapse": td_notes})
    return d


# Resolving a cell's published rows to run records is NOT a run-number lookup. Cells renumber their
# rows 1..n for display while staging ids stay non-contiguous (aws-medium-b publishes 12 rows whose
# staging runs start at 03; a number join silently loads the WRONG runs). paper_tables already does
# the correct join, through the session UUID in each row's link, so reuse it rather than repeat it.
import paper_tables as pt   # noqa: E402


def cell_records(cell: str) -> list:
    """(record, label) for every PUBLISHED row of a cell, joined by session UUID."""
    cloud, _, tier = cell.partition("-")
    tier = tier or "easy"
    suffix = "" if tier == "easy" else f"-{tier}"
    recs = pt.published_records(cloud, suffix, tier)
    return [(r, f"row{i + 1}(run{r.get('run')})") for i, r in enumerate(recs)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", default="aws-medium-a")
    ap.add_argument("--runs", default="", help="comma list; default = the cell's published runs")
    ap.add_argument("--control", action="store_true",
                    help="re-price runs already priced by the CloudTrail path and diff")
    ap.add_argument("--apply", action="store_true", help="write results/<cloud>/<cell>/REPRICE.json")
    ap.add_argument("--profile", default=None)
    a = ap.parse_args()

    regions = ["us-east-1"]        # every priced resource in these cells is us-east-1; verified
    rows = cell_records(a.cell)
    if a.runs:
        want = {int(x) for x in a.runs.split(",") if x.strip()}
        rows = [(r, lab) for r, lab in rows if r.get("run") in want]

    if a.control:
        rows = [(r, lab) for r, lab in rows
                if "cloudtrail" in str((r.get("cost_run_rate") or {}).get("discovery"))]
        if not rows:
            print("no CloudTrail-priced run in this cell to control against")
            return
        print(f"CONTROL on {a.cell}: {[lab for _, lab in rows]}")

    out = {}
    for rec, n in rows:
        old = rec.get("cost_run_rate") or {}
        new = reprice(rec, regions, a.profile)
        o = old.get("monthly_usd")
        m = new.get("monthly_usd")
        tag = "old-mechanism" if "cloudtrail" not in str(old.get("discovery")) else "CONTROL"
        if m is None:
            print(f"  {n} [{tag}] FAILED: {new.get('error')}")
        else:
            delta = "" if o is None else f"  ({(m - o) / o * 100:+.1f}%)"
            match = ""
            if tag == "CONTROL":
                match = "   MATCHES" if abs(m - (o or 0)) < 0.005 else "   *** DIFFERS ***"
            print(f"  {n:<16} [{tag}] ${o:>8.2f} -> ${m:>8.2f}{delta}"
                  f"  resources={new.get('n_resources')} unpriced={len(new.get('unpriced_resources') or [])}{match}")
        out[n] = {"old": old, "new": new}

    if a.apply:
        cloud = a.cell.split("-")[0]
        path = f"{REPO}/results/{cloud}/{a.cell}/REPRICE.json"
        json.dump(out, open(path, "w"), indent=1, sort_keys=True)
        print(f"\n[written] {path}")


if __name__ == "__main__":
    main()
