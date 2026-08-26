#!/usr/bin/env python3
"""Post-hoc failure-mode classification for the measured runs (adds a `derived` block; raw fields
stay intact). The live `outcome` string conflates two distinct first-attempt failures, so we
separate them from transcript evidence:

  success         first-attempt functional check passed
  broken-endpoint app served but >=1 endpoint failed on the first attempt (delivered-but-broken)
  premature-stop  the agent ENDED ITS TURN before the app was serving (a URL exists in the
                  transcript but the final message shows it stopped mid-build): the DeployBench
                  premature-self-stop mode, measured live
  no-deploy       no app URL anywhere in the transcript (never got as far as a deployment)

It also recovers the app URL from the WHOLE transcript (not just the final prose), which the live
harness missed on premature-stop runs, and keeps the final message tail as the evidence.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acspeed import transcript as acs  # noqa: E402
from autorun import pick_url, _BASE, all_text  # noqa: E402

WAIT_HINT = ("pick this up", "waiting on", "will finish", "build finishes", "still running",
             "still building", "come back", "in progress", "let it finish", "monitor")


def final_text(rows: list[dict]) -> str:
    for r in reversed(rows):
        if r.get("type") == "assistant":
            for b in acs._blocks(r):
                if b.get("type") == "text" and b.get("text", "").strip():
                    return b["text"]
    return ""


def classify(rec: dict) -> dict:
    tx = (rec.get("deploy") or {}).get("transcript")
    rows = acs._load_rows(tx) if tx and os.path.exists(tx) else []
    url_tx = pick_url(all_text(rows), _BASE["url_re"])["url"] if rows else None
    fin = final_text(rows)
    stopped_mid = any(h in fin.lower() for h in WAIT_HINT)
    served = bool(re.search(r"HTTP [23]\d\d", rec.get("first_check_out", "")))
    fa = rec.get("first_attempt_functional")
    if fa is True:
        mode = "success"
    elif fa is False:
        # the app answered the round-1 check. served (2xx/3xx somewhere) = up but an endpoint broke;
        # not served (all 000/5xx) = it was not up when the agent stopped.
        mode = "broken-endpoint" if served else ("premature-stop" if stopped_mid else "not-serving")
    elif url_tx:
        mode = "premature-stop"          # deployed (URL in transcript) but no check ran (url was None)
    else:
        mode = "no-deploy"
    return {"first_attempt_mode": mode, "url_in_transcript": url_tx, "served_first": served,
            "stopped_mid_build": stopped_mid, "final_msg_tail": fin[-320:]}


def main() -> None:
    d = sys.argv[1] if len(sys.argv) > 1 else \
        "/home/milos/Desktop/research_paper_data/_measurements/redu"
    for p in sorted(glob.glob(os.path.join(d, "run??.json"))):
        rec = json.load(open(p))
        if not (isinstance(rec.get("recovery"), dict) and "first_attempt_functional" in rec):
            continue
        rec["derived"] = classify(rec)
        json.dump(rec, open(p, "w"), indent=2, default=str)
        dv = rec["derived"]
        print(f"{os.path.basename(p)}: {dv['first_attempt_mode']:15s} "
              f"url_tx={dv['url_in_transcript']} stopped_mid={dv['stopped_mid_build']}")


if __name__ == "__main__":
    main()
