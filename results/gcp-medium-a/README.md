# gcp (medium tier): umami + second site + integration + durability (2026-09-01)

n=10 (run01-10). run05 was re-run 2026-09-01 with the second-site naming fix (below) and now passes
cleanly, so the earlier "run05 excluded" is resolved. The **Medium** tier is a five-operation workload,
not just a deploy: **deploy-serve** (umami provisions and serves), **register** (a probe admin user
exists), **deploy-site-b** (a standardized second site, a real Node app, serves), **integrate** (umami
tracking is wired into the second site and a REAL headless-browser visit records a pageview on a unique
sentinel path, 0 -> 1), then a **restart-durability** goal (restart, and require every durable effect to
survive). Architecture: serverless (Cloud Run) umami + managed Postgres (Cloud SQL) + a second Cloud Run
site.

**Nine of ten runs pass all five operations clean (5/5); run10 reached a working end-state (terminal
integrate and durability both pass) after the agent disabled umami's bot-check, and carries a 4/5 for the
first integrate attempt before that fix.** All ten were content-verified (real umami app content, 9673
bytes) and first-attempt live (10/10).

**The second-site collision is fixed.** The second site is now deployed under a token-carrying name (run05's
is `alcove-acs5f36c277-...`), so two runs can no longer collide on one shared Cloud Run service, and the
token-scoped teardown finds and removes it. run05, the run the old fixed-name collision broke, re-ran clean
**5/5** with a token-named site and left **zero orphans** (verified after the run via the Cloud Run,
Cloud SQL and Artifact Registry APIs: 0 each).

## Overview (total wall = all operations; agent $ = the whole workflow)

| run | total wall (s) | agent $ | $/mo @ 10k req | $/mo @ 500k req | $/mo @ 10M req | tier |
|----:|---------------:|--------:|---------------:|----------------:|---------------:|:----:|
| 1 | 2581 | $5.73 | $29.28 | $30.72 | $58.51 | 5/5 |
| 2 | 1262 | $4.09 | $47.17 | $48.60 | $76.39 | 5/5 |
| 3 | 1480 | $4.39 | $29.28 | $30.72 | $58.51 | 5/5 |
| 4 | 1541 | $4.53 | $29.28 | $30.72 | $58.51 | 5/5 |
| 5 | 2035 | $6.20 | $42.42 | $43.86 | $71.64 | 5/5 |
| 6 | 1431 | $3.74 | $29.28 | $30.72 | $58.51 | 5/5 |
| 7 | 1902 | $5.56 | $68.67 | $68.67 | $68.67 | 5/5 |
| 8 | 1938 | $5.06 | $29.28 | $30.72 | $58.51 | 5/5 |
| 9 | 1184 | $3.01 | $29.28 | $30.72 | $58.51 | 5/5 |
| 10 | 4721 | $10.79 | $68.67 | $68.67 | $68.67 | 4/5* |

*run10 reached a working end-state; the 4/5 is the first integrate attempt before the bot-check fix. Cost is
**usage-metered** (Cloud Run); most runs read the same schedule (10k -> $29.28, 500k -> $30.72, 10M ->
$58.51). A few runs landed on a different min-instance/Cloud SQL configuration and read higher, flat where
the agent held a min instance always allocated (runs 7 and 10 at $68.67). Captured 2026-09-01; egress
estimated at 50 KB average response; provisioned floor + active per-second compute folded in.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 472 | 79 | 402 | 1148 | 480 | 2581 |
| 2 | 365 | 62 | 232 | 210 | 393 | 1262 |
| 3 | 446 | 68 | 206 | 218 | 542 | 1480 |
| 4 | 461 | 76 | 336 | 230 | 439 | 1541 |
| 5 | 469 | 53 | 226 | 744 | 542 | 2035 |
| 6 | 445 | 69 | 271 | 222 | 425 | 1431 |
| 7 | 481 | 69 | 238 | 910 | 204 | 1902 |
| 8 | 485 | 69 | 250 | 712 | 422 | 1938 |
| 9 | 479 | 76 | 220 | 214 | 194 | 1184 |
| 10 | 463 | 81 | 241 | 1168 | 2768 | 4721 |

deploy-serve `t1` sits in a tight band (365 to 485s: the serverless URL is only pollable after the agent
reports it), and the total is dominated by the operations after the first deploy. deploy-site-b is mostly
platform time from a repeated provision (a fresh Cloud Run revision every run):

| run | deploy-site-b wall (s) | platform (s) | agent (s) |
|----:|-----------------------:|-------------:|----------:|
| 1 | 402 | 234 | 134 |
| 2 | 232 | 82 | 119 |
| 3 | 206 | 82 | 89 |
| 4 | 336 | 148 | 153 |
| 5 | 226 | 93 | 102 |
| 6 | 271 | 108 | 128 |
| 7 | 238 | 99 | 104 |
| 8 | 250 | 121 | 94 |
| 9 | 220 | 80 | 105 |
| 10 | 241 | 100 | 106 |

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` (external concurrent poll, first HTTP status < 500): **456.7s** [95% CI 433.5, 473.3],
= critical_platform **261.8s** + critical_agent **160.6s** (overlap 0). First-attempt liveness **10/10**,
content-verified **10/10**. Capability C is **DEFERRED**: Cloud Run is shell-less, so the off-clock
micro-probes (sysbench/STREAM/fio over SSH) do not apply, and the VM-to-VM network axis is scored only for
multi-VM operations. Efficiency (Part 3) is **DEFERRED**: no reference-optimal gold is authored for this
platform.

Teardown: every run left 0 resources (reaper found nothing to reap; every URL is 404/dead on re-check; a
post-run cloud sweep found 0 Cloud Run services, Cloud SQL instances, or Artifact Registry repos for this
batch's tokens). The token-named second site is now torn down like every other resource.

Redacted, infrastructure-neutral transcripts (one resumed session per run, covering all operations and the
teardown) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0, n=10).
