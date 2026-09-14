#!/usr/bin/env python3
"""Publish the per-run records beside the cell tables, so the paper's tables regenerate offline.

Until this existed the published artifact carried the cell tables and every session transcript, but
not the per-run records ``paper_tables.py`` actually reads, so a reader could see every number and
still not recompute one. This writes those records into
``results/<cloud>/<cloud>-<tier>/records/`` for exactly the runs each cell publishes.

Two transformations, both necessary before anything leaves the machine:

* ``transcript`` fields hold absolute paths into a private staging tree. They are rewritten to the
  published relative form, ``sessions/<uuid>.jsonl``, which is where the transcript actually sits in
  the artifact and what the cell table already links to.
* Every record is passed through the JSON-aware redactor, then the result is SCANNED with the
  independent secret and substrate scanners. A hit aborts the whole run without writing: publishing
  is one-way, so the gate fails closed.

    python3 tools/publish_records.py --dry-run   # report what would be written, write nothing
    python3 tools/publish_records.py             # write the records

ACSPEED_STAGING must point at the private per-run tree, exactly as for paper_tables.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paper_tables as pt                                   # noqa: E402
from acspeed import redact                                  # noqa: E402

UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def _rewrite_paths(o):
    """Absolute staging paths -> the published sessions/<uuid>.jsonl form. Anything else absolute
    is dropped rather than published: a path outside the artifact is not a reproducibility aid."""
    if isinstance(o, dict):
        return {k: (_transcript(v) if k == "transcript" else _rewrite_paths(v))
                for k, v in o.items()}
    if isinstance(o, list):
        return [_rewrite_paths(v) for v in o]
    return o


def _transcript(v):
    if not isinstance(v, str):
        return v
    m = UUID_RE.search(os.path.basename(v))
    return f"sessions/{m.group(1)}.jsonl" if m else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    written, findings, total = [], [], 0
    for cloud in pt.ANON:
        for tier_key, suffix, _name in pt.TIERS:
            recs = pt.published_records(cloud, suffix, tier_key)
            outdir = os.path.join(pt.RESULTS, cloud, f"{cloud}-{tier_key}", "records")
            for rec in recs:
                total += 1
                clean = _rewrite_paths(rec)
                counts: Counter = Counter()
                # run_token must survive redaction. The key ends in "token" so the redactor claims
                # it, but it is not a credential: it is the per-run name fragment deliberately
                # embedded in every resource the run creates, and it is already public in the served
                # URLs and in the transcripts. gold._is_suspect matches it against the served URL to
                # rule out a leftover deployment from an earlier run, so redacting it silently makes
                # EVERY first-poll run suspect. That drops all 32 GCP runs out of the Part 3 floor
                # and frontier, and the secret scanners cannot catch it because nothing leaked.
                token = clean.get("run_token")
                clean = redact.redact_obj(clean, counts)
                if token is not None:
                    clean["run_token"] = token
                blob = json.dumps(clean, indent=1, sort_keys=True, ensure_ascii=False)
                # Independent gate: scan the FINAL bytes, not the input, and never with the same
                # call that redacted them.
                hits = redact.scan_for_secrets(blob) + redact.scan_for_substrate(blob)
                # The scanner shares the redactor's key-name rule, so it flags the run_token we
                # deliberately kept. Allow EXACTLY this record's token and nothing else: a blanket
                # suppression here would be a gate that cannot fail, which is the failure mode this
                # gate exists to avoid.
                if token is not None:
                    allowed = f'"run_token": "{token}"'
                    hits = [h for h in hits if allowed not in str(h[1])]
                if hits:
                    findings.append((cloud, tier_key, rec.get("run"), hits[:3]))
                written.append((outdir, f"run{rec.get('run'):02d}.json", blob))

    if findings:
        print("ABORTED: the scanner found residue; nothing was written.")
        for cloud, tier, run, hits in findings:
            print(f"  {cloud}-{tier} run{run}: {hits}")
        sys.exit(1)

    print(f"scanned {total} records, 0 findings")
    if args.dry_run:
        for outdir, name, blob in written[:3]:
            print("would write", os.path.join(outdir, name), f"({len(blob)} bytes)")
        print(f"... {len(written)} files total (dry run, nothing written)")
        return

    for outdir, name, blob in written:
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as fh:
            fh.write(blob + "\n")
    print(f"wrote {len(written)} records under results/")


if __name__ == "__main__":
    main()
