# gcp (medium tier): umami + second site + integration + durability (2026-09-01)

n=9 (run01-04, run06-10; run05 excluded, see below), extended from the earlier n=3 through the same
corrected medium harness. The **Medium** tier is a five-operation workload, not just a deploy: **deploy-serve**
(umami provisions and serves), **register** (a probe admin user exists), **deploy-site-b** (a standardized
second site, a real Node app, serves), **integrate** (umami tracking is wired into the second site and a REAL
headless-browser visit records a pageview on a unique sentinel path, 0 -> 1), then a **restart-durability**
goal (restart, and require every durable effect to survive). Architecture: serverless (Cloud Run) umami +
managed Postgres (Cloud SQL) + a second Cloud Run site.

**All nine runs reached a working end-state.** Eight passed all five operations clean (5/5). run10 reached a
working end-state (terminal integrate and durability both pass: the wiring is served and the sentinel
pageview survived the restart) after the agent disabled umami's bot-check so the headless visit registered;
it carries a 4/5 for the first integrate attempt, before that fix. All nine were content-verified (real umami
app content, 9673 bytes) and first-attempt live (9/9).

**Why run05 is excluded.** The second site is deployed under a fixed name (`alcove`) and is not torn down
between runs, so runs 4 to 10 deploy onto a Cloud Run service an earlier run left behind. On run05 that
shared service was carrying revisions from another run without this run's wiring, so the harness graded a
colliding second site and integrate could not pass on this run's own work. The agent diagnosed the collision
from the Cloud Run request log and relocated to a collision-free name, but the run is not independent, so it
is left out. This is a harness teardown gap (the reaper does not tear down the fixed-name second site),
tracked separately with the universal-teardown fix. The nine kept runs include six that reused the shared
service and still passed cleanly; reuse is a run-independence caveat on those, not an outcome change, and the
provision spine (`t1`, on the token-unique umami service) is independent across all nine.

The overview is keyed on **total wall** (the whole workload), because a single operation's time (the deploy
`t1`) hides most of the run. The per-operation table breaks out where the time goes.

## Overview (total wall = all operations; agent $ = the whole workflow)

| run | total wall (s) | agent $ | $/mo @ 10k req | $/mo @ 500k req | $/mo @ 10M req | tier |
|----:|---------------:|--------:|---------------:|----------------:|---------------:|:----:|
| 1 | 2581 | $5.73 | $29.28 | $30.72 | $58.51 | 5/5 |
| 2 | 1262 | $4.09 | $47.17 | $48.60 | $76.39 | 5/5 |
| 3 | 1480 | $4.39 | $29.28 | $30.72 | $58.51 | 5/5 |
| 4 | 1541 | $4.53 | $29.28 | $30.72 | $58.51 | 5/5 |
| 6 | 1431 | $3.74 | $29.28 | $30.72 | $58.51 | 5/5 |
| 7 | 1902 | $5.56 | $68.67 | $68.67 | $68.67 | 5/5 |
| 8 | 1938 | $5.06 | $29.28 | $30.72 | $58.51 | 5/5 |
| 9 | 1184 | $3.01 | $29.28 | $30.72 | $58.51 | 5/5 |
| 10 | 4721 | $10.79 | $68.67 | $68.67 | $68.67 | 4/5* |

*run10 reached a working end-state (see above); the 4/5 is the first integrate attempt before the bot-check
fix. Cost is **usage-metered** (Cloud Run), so the monthly bill scales with traffic and most runs read the
same schedule (10k -> $29.28, 500k -> $30.72, 10M -> $58.51). A few runs landed on a different
min-instance/Cloud SQL configuration and read higher, flat where the agent held a min instance always
allocated (runs 7 and 10 at $68.67). Captured 2026-09-01; egress estimated at 50 KB average response;
provisioned floor + active per-second compute folded in.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 472 | 79 | 402 | 1148 | 480 | 2581 |
| 2 | 365 | 62 | 232 | 210 | 393 | 1262 |
| 3 | 446 | 68 | 206 | 218 | 542 | 1480 |
| 4 | 461 | 76 | 336 | 230 | 439 | 1541 |
| 6 | 445 | 69 | 271 | 222 | 425 | 1431 |
| 7 | 481 | 69 | 238 | 910 | 204 | 1902 |
| 8 | 485 | 69 | 250 | 712 | 422 | 1938 |
| 9 | 479 | 76 | 220 | 214 | 194 | 1184 |
| 10 | 463 | 81 | 241 | 1168 | 2768 | 4721 |

deploy-serve `t1` sits in a tight band (365 to 485s: the serverless URL is only pollable after the agent
reports it), and the total is dominated by the operations after the first deploy. run10's long total carries
the bot-check diagnosis and the durability re-verification, kept not trimmed. deploy-site-b is again mostly
platform time from a repeated provision (reuse did not shortcut it: every run redeployed a fresh revision):

| run | deploy-site-b wall (s) | platform (s) | agent (s) |
|----:|-----------------------:|-------------:|----------:|
| 1 | 402 | 234 | 134 |
| 2 | 232 | 82 | 119 |
| 3 | 206 | 82 | 89 |
| 4 | 336 | 148 | 153 |
| 6 | 271 | 108 | 128 |
| 7 | 238 | 99 | 104 |
| 8 | 250 | 121 | 94 |
| 9 | 220 | 80 | 105 |
| 10 | 241 | 100 | 106 |

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` (external concurrent poll, first HTTP status < 500): **455.3s** [95% CI 429.7, 473.4],
= critical_platform **261.2s** + critical_agent **159.7s** (overlap 0). First-attempt liveness **9/9**,
content-verified **9/9**. Capability C is **DEFERRED**: Cloud Run is shell-less, so the off-clock
micro-probes (sysbench/STREAM/fio over SSH) do not apply, and the VM-to-VM network axis is scored only for
multi-VM operations. Efficiency (Part 3) is **DEFERRED**: no reference-optimal gold is authored for this
platform.

Teardown: the reaper flagged no leftover Cloud SQL instance on any run and every URL is 404/dead on re-check.
The reaper is Cloud SQL-scoped and does not tear down the fixed-name second site, which is the reuse gap
above; a full cross-resource orphan sweep across the batch is tracked separately with the universal-teardown
fix.

Redacted, infrastructure-neutral transcripts (one resumed session per run, covering all operations and the
teardown) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0, n=9).
