"""Does the pricer produce every line AWS actually billed? The account answers; nothing is typed out.

WHY THIS EXISTS. "Every billable resource is priced correctly" was, until this module, an OPINION -- mine,
formed by reading the pricer's own output and judging it complete. That is the same self-certifying shape
as a fairness filter that asks the pricing tool whether it thinks it succeeded. The account already holds
the answer: Cost Explorer reports what was charged, keyed on USAGETYPE, and every SKU in the published
price list carries that same usagetype. So the two records join directly, and a resource the pricer never
reached shows up as a usagetype on the bill that the pricer never produced.

⛔ THIS IS NOT A GATE ON A RUN, and must never become one. Cost Explorer lags roughly a day and needs the
payer account, so a benchmark that waited for it would be unusable by anyone else -- the same objection
that killed billing reconciliation as a cost SOURCE. This is a test of the INSTRUMENT, run afterwards over
runs already paid for, exactly like a unit test that happens to need the internet.

WHY IT NEEDS NO LIST OF BILLABLE THINGS. ``aws_audit`` answers a related question from the agent's
transcript, and to do that it carries a table of which CLI verbs create a billable resource. Any such
table is wrong by omission for whatever it has not heard of, which is the defect that cost seven runs.
Here the set of billable things is not asserted at all: it is whatever AWS charged for.

WHAT IS DELIBERATELY EXCLUDED. C19 measures a standing hourly RUN-RATE: what the deployment costs to keep
running. Per-request and per-byte lines (``Requests-Tier1``, ``DataTransfer-Out-Bytes``, ``LCUUsage``) are
usage-metered, vary with traffic rather than with what was provisioned, and the paper reports egress
separately. They are reported here, but as OUT OF SCOPE rather than as misses, and the reason is named so
the exclusion cannot quietly widen.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Dict, List, Optional, Sequence, Tuple

# A usagetype priced per unit of TRAFFIC rather than per unit of TIME. Matched as substrings of the
# usagetype, which every service composes the same way. This is the C19 scope boundary, not a vendor list.
_METERED = ("Bytes", "Request", "LCUUsage", "CW:", "Lambda-GB-Second", "ProcessedBytes")


def billed_usagetypes(start: str, end: str, profile: Optional[str] = None) -> Dict[str, float]:
    """What AWS charged, by usagetype, over [start, end). Dates are YYYY-MM-DD; end is exclusive."""
    cmd = ["aws", "ce", "get-cost-and-usage", "--time-period", f"Start={start},End={end}",
           "--granularity", "DAILY", "--metrics", "UnblendedCost",
           "--group-by", "Type=DIMENSION,Key=USAGE_TYPE", "--region", "us-east-1", "--output", "json"]
    if profile:
        cmd += ["--profile", profile]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if out.returncode:
        raise RuntimeError(f"Cost Explorer unreadable: {out.stderr[:200]}. Refusing rather than "
                           f"reporting a conformance PASS that was never checked.")
    agg: Dict[str, float] = {}
    for period in json.loads(out.stdout or "{}").get("ResultsByTime", []):
        for group in period.get("Groups", []):
            key = group["Keys"][0]
            agg[key] = agg.get(key, 0.0) + float(group["Metrics"]["UnblendedCost"]["Amount"])
    return agg


def is_metered(usagetype: str) -> bool:
    return any(m in usagetype for m in _METERED)


def conformance(priced_usagetypes: Sequence[str], start: str, end: str,
                profile: Optional[str] = None) -> dict:
    """Compare what the pricer produced against what the account was charged.

    ``priced_usagetypes`` is every usagetype the pricer emitted for the runs covering that window. A
    billed standing-rate usagetype absent from it is a MISS: a real charge the instrument cannot see."""
    billed = billed_usagetypes(start, end, profile)
    produced = set(priced_usagetypes)
    hits, misses, metered = [], [], []
    for usagetype, usd in sorted(billed.items(), key=lambda kv: -kv[1]):
        if usd <= 0:
            continue                      # a zero line is free-tier or prorated, not evidence either way
        row = (usagetype, round(usd, 4))
        if is_metered(usagetype):
            metered.append(row)
        elif usagetype in produced:
            hits.append(row)
        else:
            misses.append(row)
    return {"ok": not misses, "priced": hits, "missing": misses, "out_of_scope_metered": metered,
            "produced": sorted(produced), "window": f"{start}..{end}"}


def tokens_in(results_dir: str, start: str, end: str) -> List[str]:
    """Every run token in a results directory whose run happened inside the window.

    A conformance check comparing SOME runs against a WHOLE account's bill reports the other runs'
    charges as misses, which is noise that hides the real ones. Reading the tokens from the directory
    makes the comparison complete by construction rather than by me remembering to list them."""
    import glob
    out = []
    for path in sorted(glob.glob(os.path.join(results_dir, "run*.json"))):
        try:
            with open(path) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        token, when = rec.get("run_token"), str(rec.get("measured_at") or "")[:10]
        if token and (not when or start <= when < end):
            out.append(token)
    return out


def _main(argv: List[str]) -> int:
    if len(argv) < 3:
        print("usage: python -m acspeed.aws_conformance START END (TOKEN[,TOKEN...] | RESULTS_DIR) "
              "[aws-profile]\n"
              "  dates YYYY-MM-DD, END exclusive. Given a directory, every run token measured inside\n"
              "  the window is read from it, so the comparison against the account's bill is complete.")
        return 2
    start, end = argv[0], argv[1]
    if os.path.isdir(argv[2]):
        tokens = tokens_in(argv[2], start, end)
        print(f"{len(tokens)} run tokens in {argv[2]} for {start}..{end}")
    else:
        tokens = [t for t in argv[2].split(",") if t]
    profile = argv[3] if len(argv) > 3 else None
    from acspeed.adapters.aws_ct_cost import (billable_parts, discover, enabled_regions,
                                              price_resource_universal)
    regions = enabled_regions(profile)
    produced: List[str] = []
    for token in tokens:
        resources, _unclassified = discover(f"{start}T00:00:00Z", f"{end}T00:00:00Z", token,
                                            regions, profile)
        for resource in resources:
            for part in billable_parts(resource):
                got = price_resource_universal(part, profile=profile)
                if got.get("priced"):
                    produced.append(got["usagetype"])
        print(f"  {token}: {len(resources)} resources, {len(produced)} priced lines so far")
    report = conformance(produced, start, end, profile)
    print(f"\nCONFORMANCE {report['window']}: {'PASS' if report['ok'] else 'FAIL'}")
    for usagetype, usd in report["priced"]:
        print(f"  ok      {usagetype:<44} ${usd}")
    for usagetype, usd in report["missing"]:
        print(f"  MISSING {usagetype:<44} ${usd}   <- billed, never priced")
    for usagetype, usd in report["out_of_scope_metered"]:
        print(f"  (usage) {usagetype:<44} ${usd}   metered, outside the C19 standing rate")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
