# azure (medium tier): umami + second site + integration + durability (2026-09-01)

n=10 (run01-10), extended from the earlier n=3. The **Medium** tier is a five-operation workload:
**deploy-serve** (umami provisions and serves), **register** (a probe admin user exists),
**deploy-site-b** (a standardized second site, a real Node app, serves), **integrate** (umami tracking is
wired into the second site and a REAL headless-browser visit records a pageview on a unique sentinel
path, 0 -> 1), then a **restart-durability** goal (restart, require every durable effect to survive).
**All 10 runs passed all five operations** (5/5), were durable on cycle 1, content-verified (the served
root is real umami app content, 9673 bytes), and first-attempt live (10/10). Architecture: serverless
containers (Azure Container Apps) umami, plus a second Container Apps site.

Two operational notes, both measured:
- **Teardown is flaky on the agent side: 6 of 10 runs left a resource group that the harness reaper
  then deleted** (`reaped: [rg-...]`). Every run ended fully torn down (the URL is dead on re-check),
  but only because the reaper caught the agent's incomplete teardown. Post-batch cloud-API sweep: 0
  container apps, 0 leftover resource groups (2 empty orphan RGs from this batch were removed).
- azure is the slowest platform in the suite: time-to-serving averaged ~1828s (run03 took 2977s).

## Overview (total wall = all operations; agent $ = the whole workflow)

| run | total wall (s) | agent $ | reaper needed | $/mo @ 10k | @ 500k | @ 10M |
|----:|---------------:|--------:|:-------------:|-----------:|-------:|------:|
| 1 | 2259 | $3.88 | no | $40.87 | $42.31 | $70.34 |
| 2 | 2902 | $2.68 | yes | $40.87 | $42.31 | $70.34 |
| 3 | 4605 | $3.21 | no | $40.87 | $42.31 | $70.34 |
| 4 | 3162 | $2.77 | yes | $40.87 | $42.31 | $70.34 |
| 5 | 1992 | $4.29 | no | $40.87 | $42.31 | $70.34 |
| 6 | 2867 | $3.04 | yes | $40.87 | $42.31 | $70.34 |
| 7 | 3667 | $3.67 | yes | $40.87 | $42.31 | $70.34 |
| 8 | 3014 | $7.51 | no | $40.87 | $42.31 | $70.34 |
| 9 | 2502 | $3.40 | yes | $40.87 | $42.31 | $70.34 |
| 10 | 1392 | $13.64 | yes | $40.87 | $42.31 | $70.34 |

Cost is **usage-metered** (Azure Container Apps), the same dated-list schedule every run: 10k req/mo
-> $40.87, 500k -> $42.31, 10M -> $70.34 (egress at 50 KB average response; provisioned floor + active
per-second compute folded in). Captured 2026-09-01. (agent $ outliers: run08 register 550s, run10 a
high-step run, both kept not trimmed.)

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 1221 | 40 | 361 | 87 | 550 | 2259 |
| 2 | 2252 | 51 | 209 | 177 | 213 | 2902 |
| 3 | 2977 | 325 | 924 | 197 | 182 | 4605 |
| 4 | 2201 | 54 | 461 | 168 | 278 | 3162 |
| 5 | 1350 | 50 | 323 | 85 | 184 | 1992 |
| 6 | 1996 | 48 | 307 | 162 | 354 | 2867 |
| 7 | 2521 | 316 | 383 | 209 | 238 | 3667 |
| 8 | 1503 | 550 | 357 | 235 | 369 | 3014 |
| 9 | 1521 | 67 | 467 | 192 | 255 | 2502 |
| 10 | 740 | 64 | 271 | 160 | 157 | 1392 |

As at n=3, azure's total is dominated by the **first deploy (t1)**, not deploy-site-b: the Container
Apps provision itself is slow (740 to 2977s), while the serverless second-site provision is cheap.

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` (external concurrent poll, first HTTP status < 500): **~1828s** mean [740, 2977]
(n=10). First-attempt liveness **10/10**, content-verified **10/10**. Platform/agent decomposition was
produced for 9 of 10 runs (run03's split not computed; its t1 is measured and all its ops passed):
critical_platform mean ~1374s, critical_agent mean ~314s. Capability C is **DEFERRED** (Azure Container
Apps is shell-less, so the off-clock micro-probes over SSH do not apply; the VM-to-VM network axis is
scored only for multi-VM operations). Efficiency (Part 3) floor-ratio around 1.2 to 2.3x per run against
the min-observed platform floor.

Redacted, infrastructure-neutral transcripts (one resumed session per run, covering all operations and
the teardown) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0).
