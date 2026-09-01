# gcp (medium tier): umami + second site + integration + durability (2026-08-31)

n=3 (run01-03), the fresh batch through the corrected medium harness. The **Medium** tier is a five-operation
workload, not just a deploy: **deploy-serve** (umami provisions and serves), **register** (a probe admin user
exists), **deploy-site-b** (a standardized second site, a real Node app, serves), **integrate** (umami tracking
is wired into the second site and a REAL headless-browser visit records a pageview on a unique sentinel path,
0 -> 1), then a **restart-durability** goal (restart, and require every durable effect to survive). Every run
below passed all five operations, was **durable on cycle 1**, was **content-verified** (the served root is real
umami app content, 9673 bytes), and was **torn down clean** (no orphans; the URL is 404/dead on re-check).
Architecture: serverless (Cloud Run) umami + managed Postgres + a second Cloud Run site.

Each run uses a **unique** per-run service name (`umami-acs<token>`), so no leftover deployment can answer for
another run. Because a Cloud Run URL is not knowable until the agent deploys, the external poller can only begin
once the URL exists; the first poll then succeeds, and post-teardown the URL is 404 (the service this run
created was the one that answered, and it was deleted).

The overview is keyed on **total wall** (the whole workload), because a single operation's time (the deploy
`t1`) hides most of the run. The per-operation table breaks out where the time goes.

## Overview (total wall = all operations; agent $ = the whole workflow)

| run | total wall (s) | agent $ | $/mo @ 10k req | $/mo @ 500k req | $/mo @ 10M req |
|----:|---------------:|--------:|---------------:|----------------:|---------------:|
| 1 | 2581 | $5.73 | $29.28 | $30.72 | $58.51 |
| 2 | 1262 | $4.09 | $29.28 | $30.72 | $58.51 |
| 3 | 1480 | $4.39 | $29.28 | $30.72 | $58.51 |

Cost is **usage-metered** (Cloud Run), not a fixed VM, so the monthly bill scales with traffic and is the same
schedule for every run (a function of the dated list price, not the run's wall). Schedule: 10k req/mo ->
$29.28, 500k -> $30.72, 10M -> $58.51 (egress estimated at 50 KB average response; provisioned floor + active
per-second compute folded in). Captured 2026-08-31.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 472 | 79 | 402 | 1148 | 480 | 2581 |
| 2 | 365 | 62 | 232 | 210 | 393 | 1262 |
| 3 | 446 | 68 | 206 | 218 | 542 | 1480 |

deploy-serve `t1` is higher than on a fixed VM (the serverless URL is only pollable after the agent reports it),
and the total is dominated by the operations after the first deploy (integrate on run01 was a 1148s agent-side
outlier, kept not trimmed):

| run | deploy-site-b wall (s) | platform (s) | agent (s) |
|----:|-----------------------:|-------------:|----------:|
| 1 | 402 | 234 | 134 |
| 2 | 232 | 82 | 119 |
| 3 | 206 | 82 | 89 |

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` (external concurrent poll, first HTTP status < 500): **427.8s** [95% CI 365.3, 472.2],
= critical_platform **252.7s** + critical_agent **139.9s** (overlap 0). First-attempt liveness **3/3**,
content-verified **3/3**. Capability C is **DEFERRED**: Cloud Run is shell-less, so the off-clock micro-probes
(sysbench/STREAM/fio over SSH) do not apply, and the VM-to-VM network axis is scored only for multi-VM
operations. Efficiency (Part 3) is **DEFERRED**: no reference-optimal gold is authored for this platform.

Redacted, infrastructure-neutral transcripts (one resumed session per run, covering all operations and the
teardown) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0).
