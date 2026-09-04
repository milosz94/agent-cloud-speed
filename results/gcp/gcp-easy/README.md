# gcp: Easy-tier umami (2026-09-04)

n=12 (run01 to run04 and run09 to run16 in the raw batch; run05 to run08 predate the corrected cost tool
and are not published). Architecture on every run: **Cloud Run + Cloud SQL + public IPv4** (serverless: a
usage-metered schedule, not a standing hourly rate).

Rows are labelled by their **raw run id**, not renumbered, so a row here maps directly to the run
directory it came from.

| run | t1 (s) | platform (s) | agent (s) | steps | tokens | agent $ | $/mo @ 10k req | $/mo @ 500k req | $/mo @ 10M req |
|----:|-------:|-------------:|----------:|------:|-------:|--------:|---------------:|----------------:|---------------:|
| 1 | 484.1 | 323.3 | 160.8 | 20 | 23960 | $2.43 | $29.28 | $30.72 | $58.51 |
| 2 | 1456.5 | 1300.4 | 156.1 | 18 | 19965 | $1.95 | $29.28 | $30.72 | $58.51 |
| 3 | 797.5 | 637.7 | 159.8 | 19 | 25582 | $2.34 | $68.67 | $68.67 | $68.67 |
| 4 | 507.1 | 327.0 | 180.1 | 21 | 20109 | $1.61 | $29.28 | $30.72 | $58.51 |
| 9 | 730.0 | 527.5 | 202.5 | 32 | 29417 | $2.88 | $29.28 | $30.72 | $58.51 |
| 10 | 754.4 | 621.2 | 133.2 | 20 | 17080 | $1.98 | $29.28 | $30.72 | $58.51 |
| 11 | 795.7 | 622.1 | 173.6 | 23 | 23866 | $2.77 | $29.28 | $30.72 | $58.51 |
| 12 | 761.8 | 553.9 | 207.9 | 28 | 28509 | $2.76 | $16.15 | $18.06 | $54.97 |
| 13 | 720.9 | 550.7 | 170.2 | 23 | 19856 | $1.96 | $29.28 | $30.72 | $58.51 |
| 14 | 1311.9 | 1065.0 | 246.9 | 29 | 33107 | $3.12 | $16.15 | $18.06 | $54.97 |
| 15 | 850.9 | 628.0 | 222.9 | 28 | 27310 | $2.59 | $29.28 | $30.72 | $58.51 |
| 16 | 478.7 | 264.2 | 214.5 | 29 | 28327 | $2.68 | $29.28 | $30.72 | $58.51 |

**The whole table was recomputed on 2026-09-04 with one stated composition**, so every row is comparable:
`platform` and `agent` are the current tool's critical-path split (`critical_platform_s` /
`critical_agent_s`), `tokens` is output tokens over the whole workflow (deploy + teardown), and
`agent $` is the whole workflow (deploy round + deprovision). The earlier table quoted a narrower
deploy-only composition and slightly different split values; those are superseded here, not corrected in
place, and remain in git history.

Cost is **usage-metered**, so the bill rises with traffic. Most runs read the same schedule
(**$29.28/mo at 10k up to $58.51/mo at 10M req**). Three runs differ by the agent's own configuration
choice: run03 used instance-based billing (`--no-cpu-throttling`), flat at **$68.67/mo** at any traffic;
runs 12 and 14 landed on a cheaper floor (**$16.15/mo at 10k**) that still converges to roughly the same
figure at 10M. Captured on each run's date; egress estimated at a 50 KB average response.

## Provision spine (Part 1/2)

Time-to-serving `t1` mean **804.1s** [95% CI 653.1, 977.3] (n=12), = critical_platform **618.4s**
[470.6, 791.1] + critical_agent **185.7s** [168.3, 203.8], overlap 0. Range 478.7 to 1456.5s.
First-attempt liveness **12/12**, content-verified **12/12**.

Capability C is **N/A, disclosed**: Cloud Run is shell-less, so the off-clock micro-probes cannot run.
This is a legitimate substrate the method cannot probe, not a missing measurement.

Efficiency (Part 3): floor-ratio **3.0x** [95% CI 2.5, 3.7] (n=12) against F_C = 264.2s (min observed
critical-platform floor); bracket [264.2, 478.7]s = 1.812x wide, floor sensitivity 1.647 to 2.013 at
F_C(1 +/- 0.1). Selection excess 0 by construction (one-operation provision gold). Gold
`provision-deploy/1.1.0`.

**Every run in this cell serves on the first poll, and all twelve are admitted.** A Cloud Run deploy
does not hand back the service URL until the revision is already live, so the poller cannot observe a
not-yet-serving state however early it starts, and `t1` is an honest upper bound rather than a
false-early stop. Gold 1.0.0 excluded any first-poll run and therefore admitted **none** of this cloud's
runs to the Part 3 floor or frontier at any n; 1.1.0 excludes one only when the clock was stopped by a
4xx (a platform edge answering before the app) or when the served URL does not carry the run's own token
(a possible leftover deployment). All twelve stopped on a real 200 with their own tokens in the URL.

Redacted, infrastructure-neutral transcripts are in `sessions/`; see `sessions/REDACTION-MANIFEST.json`
(residue 0).
