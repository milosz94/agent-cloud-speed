#!/usr/bin/env python3
"""Correct the published AWS records for the 2026-10-03 pricer fix (acspeed/adapters/aws_ct_cost.py: a price found only
by the whole-disclosure sweep must denote the resource itself; a per-event unit has no standing hourly rate).

63 of the 261 AWS cost lines (26 of 32 records) were another product's rate, billed FOR a resource rather than the
resource: VPC Network Access Analyzer per network interface assessed, IoT positioning per position solved (a target
group), DevOps Guru per analysed CloudFront distribution or S3 bucket per hour. None of those products was used in any
run. CloudTrail for the runs is no longer retained, so this applies the fixed rules to the stored lines: each such
component and its priced_resources entry are removed, its resource is listed under no_sku_match with the reason, and the
totals are recomputed the way the pricer writes them (all_in_hourly_usd to 6 places, monthly_usd = all_in x 730 to 4,
each usage level to cents). The removed lines and the totals before the correction are kept in the record under
removed_2026_10_03. A record already corrected without that pruning (release commit ff5f886) has its priced_resources
pruned by the four foreign usagetypes.

    python3 tools/drop_foreign_aws_lines.py [--check]     # --check: report, change nothing
"""
import glob, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOREIGN = {"Position": "IoT positioning per position solved",
           "NetworkInterface-Assessment": "VPC Network Access Analyzer per interface assessed",
           "usagehours": "DevOps Guru per analysed resource-hour"}


FOREIGN_USAGETYPES = {"USE1-Solved-IP-Positions", "USE1-NetworkInterface-Assessment",
                      "USE1-CloudFront-Distribution-GroupB", "USE1-S3-Bucket-GroupA"}


def correct(rec):
    """Remove the foreign lines from one record; return how many components and priced_resources entries went."""
    c = rec.get("cost_run_rate") or {}
    comps, priced = c.get("components", []), c.get("priced_resources", [])
    if not any(x.get("native_unit") in FOREIGN for x in comps):
        stale = [x for x in priced if x.get("usagetype") in FOREIGN_USAGETYPES]
        if stale:
            c["priced_resources"] = [x for x in priced if x.get("usagetype") not in FOREIGN_USAGETYPES]
        return 0, len(stale)
    # The pricer appends a component and its priced_resources entry together, so before this correction they pair by
    # position; refuse a record where they do not.
    if len(comps) != len(priced) or any(
            x.get("name") != y.get("kind") or (x.get("native_unit") in FOREIGN) != (y.get("usagetype") in FOREIGN_USAGETYPES)
            for x, y in zip(comps, priced)):
        raise SystemExit("components and priced_resources do not pair in run %s" % rec.get("run"))
    pairs = list(zip(comps, priced))
    gone = [(x, y) for x, y in pairs if x["native_unit"] in FOREIGN]
    removed = {"reason": "another product's rate, not this resource's own line (pricer fix of 2026-10-03)",
               "all_in_hourly_usd_before": c.get("all_in_hourly_usd"), "monthly_usd_before": c.get("monthly_usd"),
               "lines": [dict(y, native_unit=x["native_unit"], raw_unit_price=x.get("raw_unit_price"),
                              hourly_usd=x["hourly_usd"]) for x, y in gone]}
    c["components"] = [x for x, _y in pairs if x["native_unit"] not in FOREIGN]
    c["priced_resources"] = [y for x, y in pairs if x["native_unit"] not in FOREIGN]
    note = c.setdefault("no_sku_match", [])
    for x, _y in gone:
        note.append("%s (another product's rate, not this resource's own line: %s; removed 2026-10-03)"
                    % (x.get("name"), FOREIGN[x["native_unit"]]))
    c["all_in_hourly_usd"] = round(sum(x["hourly_usd"] for x in c["components"]), 6)
    c["monthly_usd"] = round(c["all_in_hourly_usd"] * 730, 4)
    te = c.get("traffic_estimate")
    if isinstance(te, dict):
        for k in te:
            te[k] = round(c["monthly_usd"], 2)
    c["removed_2026_10_03"] = removed
    return len(gone), len(gone)


COST_COLS = {"fixed $/mo": lambda c: c["monthly_usd"], "$/mo @ 10k": lambda c: c["traffic_estimate"]["low"],
             "$/mo @ 500k": lambda c: c["traffic_estimate"]["medium"], "$/mo @ 10M": lambda c: c["traffic_estimate"]["high"]}


def update_readme(cell_dir, by_session):
    """Rewrite the cost cells of the corrected runs' README rows from their records (to cents, as published)."""
    p = os.path.join(cell_dir, "README.md")
    lines = open(p).read().split("\n")
    head = None
    changed = 0
    for i, line in enumerate(lines):
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if "fixed $/mo" in cells:
            head = cells
            continue
        m = re.search(r"sessions/([0-9a-f-]+)\.jsonl", line)
        if head and m and m.group(1) in by_session and len(cells) == len(head):
            c = by_session[m.group(1)]["cost_run_rate"]
            for name, get in COST_COLS.items():
                if name in head:
                    cells[head.index(name)] = "$%.2f" % round(get(c), 2)
            new = "| " + " | ".join(cells) + " |"
            if new != line:
                lines[i] = new
                changed += 1
    open(p, "w").write("\n".join(lines))
    return changed


def main():
    check = "--check" in sys.argv
    lines = recs = entries = 0
    for p in sorted(glob.glob(os.path.join(ROOT, "results", "aws", "aws-*", "records", "run*.json"))):
        rec = json.load(open(p))
        n, e = correct(rec)
        if n or e:
            lines += n
            entries += e
            recs += 1
            if not check:
                tail = "\n" if open(p).read().endswith("\n") else ""
                open(p, "w").write(json.dumps(rec, indent=1) + tail)    # the records' own serialisation
    rows = 0
    if not check:
        for d in sorted(glob.glob(os.path.join(ROOT, "results", "aws", "aws-*"))):
            by_session = {}
            for p in glob.glob(os.path.join(d, "records", "run*.json")):
                rec = json.load(open(p))
                for rd in rec.get("rounds") or []:
                    if rd.get("session"):
                        by_session[rd["session"]] = rec
            rows += update_readme(d, by_session)
    print("%s %d lines and %d priced_resources entries in %d records; README rows rewritten: %d"
          % ("would remove" if check else "removed", lines, entries, recs, rows))


if __name__ == "__main__":
    main()
