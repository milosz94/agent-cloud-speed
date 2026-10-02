"""Re-derive the deploy-leg split of the 32 published Easy runs, as it was re-derived on 2026-08-30.

On that date the labeler's 300 s idle cap was amended so that a gap ending in a platform event is platform time at
any length, and the Easy runs, all measured before it, had their deploy split re-derived. The re-derivation anchored
each leg at its first transcript event: the leg spans t1 - t0 (the record's ``time_to_serving_s``) from that event,
so the transcript is clipped at ``first event + time_to_serving_s``, its gaps are labeled by the released rule
(``acspeed.transcript._spans_from_rows`` with the default cap, outside an operation window's idle rule), and the
time from the last clipped event to that instant is one trailing platform span (``post_handoff_boot_s``).

The harness itself anchors a leg at its own t0, which is not in the records, so this anchor lived only in a one-off
script until this release (Part 5, section 3 dates the amendment). This tool regenerates every one of the 32 splits
from the released records and transcripts and compares it with the published value.

Usage: python tools/easy_split_rederive.py      (ACSPEED_STAGING, if set, should be an empty directory)
"""
import glob, json, os, re, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from acspeed import transcript as T  # noqa: E402

CLOUDS = ["aws", "gcp", "azure"]
ROW = re.compile(r"^\|\s*\[\d+\]\(sessions/([0-9a-f-]+)\.jsonl\)", re.M)
FIELDS = ("critical_platform_s", "critical_agent_s", "makespan_s", "post_handoff_boot_s")
TOL = 0.15  # the records print one decimal


def rederive(transcript, t1):
    rows = T._load_rows(transcript)
    serving = T._epoch(rows[0]) + t1
    clipped = T.clip_rows(rows, serving)
    spans = T._spans_from_rows(clipped, T.DEFAULT_IDLE_CAP)
    boot = max(0.0, serving - T._epoch(clipped[-1]))
    cp = sum(s.duration for s in spans if s.owner == T.PLATFORM) + boot
    ca = sum(s.duration for s in spans if s.owner == T.AGENT)
    return round(cp, 1), round(ca, 1), round(cp + ca, 1), round(boot, 1)


def main():
    ok = n = 0
    for c in CLOUDS:
        d = os.path.join(REPO, "results", c, f"{c}-easy")
        published = ROW.findall(open(os.path.join(d, "README.md")).read())
        by_session = {}
        for p in glob.glob(os.path.join(d, "records", "run*.json")):
            r = json.load(open(p))
            for rd in r.get("rounds") or []:
                if rd.get("session"):
                    by_session[rd["session"]] = (os.path.basename(p), r)
        for u in published:
            name, r = by_session[u]
            got = rederive(os.path.join(d, r["deploy"]["transcript"]), r["time_to_serving_s"])
            want = tuple(r["split"][f] for f in FIELDS)
            same = all(abs(a - b) <= TOL for a, b in zip(got, want))
            ok += same
            n += 1
            print("%-5s %s  re-derived %s  published %s  %s" % (c, name, got, want, "OK" if same else "DIFF"))
    print("%d of %d published Easy deploy splits reproduce (platform, agent, makespan, trailing boot; within %.2f s)"
          % (ok, n, TOL))
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
