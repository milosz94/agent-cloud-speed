# azure (medium tier): umami + second site + integration + durability (2026-09-01)

n=3 (run01-03), the fresh batch through the corrected medium harness. The **Medium** tier is a five-operation
workload, not just a deploy: **deploy-serve** (umami provisions and serves), **register** (a probe admin user
exists), **deploy-site-b** (a standardized second site, a real Node app, serves), **integrate** (umami tracking
is wired into the second site and a REAL headless-browser visit records a pageview on a unique sentinel path,
0 -> 1), then a **restart-durability** goal (restart, and require every durable effect to survive). Every run
below passed all five operations, was **durable on cycle 1**, was **content-verified** (the served root is real
umami app content, 9673 bytes), and was **torn down clean** (no orphans; run02 reaped resource group
`rg-acsaee172e3`; the URL is dead on re-check). Architecture: serverless containers (Azure Container Apps)
umami, plus a second site (Container Apps, occasionally App Service).

azure is the **slowest** platform in this suite by a wide margin: time-to-serving averaged ~2150s (run03 took
2977s, about 50 minutes), almost all of it platform provisioning time.

The overview is keyed on **total wall** (the whole workload), because a single operation's time (the deploy
`t1`) hides most of the run. The per-operation table breaks out where the time goes.

## Overview (total wall = all operations; agent $ = the whole workflow)

| run | total wall (s) | agent $ | $/mo @ 10k req | $/mo @ 500k req | $/mo @ 10M req |
|----:|---------------:|--------:|---------------:|----------------:|---------------:|
| 1 | 2259 | $3.88 | $40.87 | $42.31 | $70.34 |
| 2 | 2902 | $2.68 | $40.87 | $42.31 | $70.34 |
| 3 | 4605 | $3.21 | $40.87 | $42.31 | $70.34 |

Cost is **usage-metered** (Azure Container Apps), not a fixed VM, so the monthly bill scales with traffic and is
the same schedule for every run (a function of the dated list price, not the run's wall). Schedule: 10k req/mo
-> $40.87, 500k -> $42.31, 10M -> $70.34 (egress estimated at 50 KB average response; provisioned floor +
active per-second compute folded in). Captured 2026-09-01.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 1221 | 40 | 361 | 87 | 550 | 2259 |
| 2 | 2252 | 51 | 209 | 177 | 213 | 2902 |
| 3 | 2977 | 325 | 924 | 197 | 182 | 4605 |

Unlike the other clouds, azure's total is dominated by the **first deploy (t1)**, not by deploy-site-b: the
Container Apps provision itself is slow (1221 to 2977s). deploy-site-b is comparatively cheap (serverless
repeat), though run03's 924s is an outlier:

| run | deploy-site-b wall (s) | platform (s) | agent (s) |
|----:|-----------------------:|-------------:|----------:|
| 1 | 361 | 227 | 118 |
| 2 | 209 | 89 | 105 |
| 3 | 924 | 772 | 134 |

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` (external concurrent poll, first HTTP status < 500): per run **1221 / 2252 / 2977s**
(mean ~2150s, n=3). First-attempt liveness **3/3**, content-verified **3/3**. The platform/agent decomposition
was produced for two of the three runs: critical_platform [974, 1975]s, critical_agent [229, 263]s (run03's
split was not computed; its t1 is measured and all its operations passed). Capability C is **DEFERRED**: Azure
Container Apps is a container service (shell-less), so the off-clock micro-probes (sysbench/STREAM/fio over SSH)
do not apply, and the VM-to-VM network axis is scored only for multi-VM operations. Efficiency (Part 3)
floor-ratio ~1.8x against the min-observed platform floor.

Redacted, infrastructure-neutral transcripts (one resumed session per run, covering all operations and the
teardown) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0).
