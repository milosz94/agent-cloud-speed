#!/usr/bin/env python3
"""Correct the published AWS records for the 2026-10-03 pricer fix (acspeed/adapters/aws_ct_cost.py: a price found only
by the whole-disclosure sweep must denote the resource itself; a per-event unit has no standing hourly rate).

63 of the 261 AWS cost lines (26 of 32 records) were another product's rate, billed FOR a resource rather than the
resource: VPC Network Access Analyzer per network interface assessed, IoT positioning per position solved (a target
group), DevOps Guru per analysed CloudFront distribution or S3 bucket per hour. None of those products was used in any
run. CloudTrail for the runs is no longer retained, so this applies the fixed rules to the stored lines: each such
component is removed, its resource is listed under no_sku_match with the reason, and the totals are recomputed the way
the pricer writes them (all_in_hourly_usd to 6 places, monthly_usd = all_in x 730 to 4, each usage level to cents).

    python3 tools/drop_foreign_aws_lines.py [--check]     # --check: report, change nothing
"""
import glob, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOREIGN = {"Position": "IoT positioning per position solved",
           "NetworkInterface-Assessment": "VPC Network Access Analyzer per interface assessed",
           "usagehours": "DevOps Guru per analysed resource-hour"}


def correct(rec):
    c = rec.get("cost_run_rate") or {}
    keep = [x for x in c.get("components", []) if x.get("native_unit") not in FOREIGN]
    gone = [x for x in c.get("components", []) if x.get("native_unit") in FOREIGN]
    if not gone:
        return 0
    c["components"] = keep
    note = c.setdefault("no_sku_match", [])
    for x in gone:
        note.append("%s (another product's rate, not this resource's own line: %s; removed 2026-10-03)"
                    % (x.get("name"), FOREIGN[x["native_unit"]]))
    c["all_in_hourly_usd"] = round(sum(x["hourly_usd"] for x in keep), 6)
    c["monthly_usd"] = round(c["all_in_hourly_usd"] * 730, 4)
    te = c.get("traffic_estimate")
    if isinstance(te, dict):
        for k in te:
            te[k] = round(c["monthly_usd"], 2)
    return len(gone)


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
    lines = recs = 0
    for p in sorted(glob.glob(os.path.join(ROOT, "results", "aws", "aws-*", "records", "run*.json"))):
        rec = json.load(open(p))
        n = correct(rec)
        if n:
            lines += n
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
    print("%s %d lines in %d records; README rows rewritten: %d" % ("would remove" if check else "removed", lines, recs, rows))


if __name__ == "__main__":
    main()
