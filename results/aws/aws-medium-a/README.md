# aws (medium tier): umami + second site + integration + durability (2026-09-01)

n=10 (run01-10), extended from the earlier n=3 through the same corrected medium harness. The **Medium**
tier is a five-operation workload, not just a deploy: **deploy-serve** (umami provisions and serves),
**register** (a probe admin user exists), **deploy-site-b** (a standardized second site, a real Node app,
serves), **integrate** (umami tracking is wired into the second site and a REAL headless-browser visit
records a pageview on a unique sentinel path, 0 -> 1), then a **restart-durability** goal (restart, and
require every durable effect to survive). Architecture: serverless containers (ECS Fargate) umami + managed
Postgres (RDS) + a load balancer, plus a second Fargate site.

**Every run reached a working end-state.** Eight of ten passed all five operations clean (5/5). Two (run06,
run08) reached a working end-state after the agent diagnosed and fixed a fair obstacle that the harness
checker had already sampled mid-fix, so they carry a partial harness score (3/5, 4/5) that understates the
delivered result; both are detailed under "The two partial-score runs" below. All ten were content-verified
(the served root is real umami app content, 9673 bytes) and first-attempt live (10/10). Each run used a
**unique** per-run load-balancer name (`umami-acs<token>`) and a unique second-site distribution, so no
leftover deployment could answer for another run.

The overview is keyed on **total wall** (the whole workload), because a single operation's time (the deploy
`t1`) hides most of the run. The per-operation table breaks out where the time goes.

## Overview (total wall = all operations; agent $ = the whole workflow)

| run | total wall (s) | agent $ | fixed $/mo | $/mo @ 10k req | $/mo @ 500k req | $/mo @ 10M req | tier |
|----:|---------------:|--------:|-----------:|---------------:|----------------:|---------------:|:----:|
| 1 | 3119 | $8.27 | $66.45 | $66.45 | $66.45 | $66.45 | 5/5 |
| 2 | 2767 | $14.80 | $48.43 | $48.43 | $48.43 | $48.43 | 5/5 |
| 3 | 2712 | $4.35 | $66.45 | $66.45 | $66.45 | $66.45 | 5/5 |
| 4 | 3856 | $8.51 | $66.45 | $66.45 | $66.45 | $66.45 | 5/5 |
| 5 | 3693 | $8.49 | $48.43 | $48.43 | $48.43 | $48.43 | 5/5 |
| 6 | 7275 | $15.39 | $66.45 | $66.45 | $66.45 | $66.45 | 3/5* |
| 7 | 2745 | $18.08 | $66.45 | $66.45 | $66.45 | $66.45 | 5/5 |
| 8 | 4204 | $12.01 | $52.47 | $52.47 | $52.47 | $52.47 | 4/5* |
| 9 | 2572 | $7.76 | $48.43 | $48.43 | $48.43 | $48.43 | 5/5 |
| 10 | 3793 | $6.49 | $66.45 | $66.45 | $66.45 | $66.45 | 5/5 |

*run06 and run08 reached a working end-state (see below); the partial score is a checker-timing artifact,
not a failed delivery. Cost is a standing **run-rate** (a Fargate task + RDS instance + load balancer run
continuously), so the bill does not scale with traffic and the three traffic columns equal the fixed floor.
Runs that landed on a smaller/cheaper task read $48.43 or $52.47. Dated AWS public list price (Price List
Query API), captured 2026-09-01; egress separate; some ancillary AWS resources (listener, target-group,
log-group, public-IPv4) are excluded by the cost tool.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 666 | 51 | 1013 | 810 | 579 | 3119 |
| 2 | 750 | 32 | 455 | 1050 | 480 | 2767 |
| 3 | 1120 | 39 | 379 | 566 | 608 | 2712 |
| 4 | 759 | 48 | 517 | 1651 | 881 | 3856 |
| 5 | 1664 | 48 | 418 | 630 | 933 | 3693 |
| 6 | 850 | 38 | 917 | 1867 | 3603 | 7275 |
| 7 | 701 | 47 | 703 | 597 | 697 | 2745 |
| 8 | 276 | 33 | 509 | 1172 | 2213 | 4204 |
| 9 | 931 | 29 | 398 | 841 | 372 | 2572 |
| 10 | 855 | 43 | 338 | 1885 | 672 | 3793 |

aws is the slowest platform in this suite, and it is **platform-bound** on the provision: deploy `t1` alone
runs 276 to 1664s. The two long totals (run06 7275s, run08 4204s) carry the extra diagnosis-and-fix time on
integrate/durability described below, kept not trimmed. deploy-site-b is again mostly platform time from a
repeated provision:

| run | deploy-site-b wall (s) | platform (s) | agent (s) |
|----:|-----------------------:|-------------:|----------:|
| 1 | 1013 | 665 | 336 |
| 2 | 455 | 96 | 343 |
| 3 | 379 | 170 | 198 |
| 4 | 517 | 166 | 339 |
| 5 | 418 | 178 | 229 |
| 6 | 917 | 490 | 416 |
| 7 | 703 | 263 | 430 |
| 8 | 509 | 143 | 356 |
| 9 | 398 | 152 | 236 |
| 10 | 338 | 112 | 215 |

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` (external concurrent poll, first HTTP status < 500): **857.3s** [95% CI 672.4, 1075.5],
= critical_platform **543.2s** + critical_agent **304.2s** (overlap 0). First-attempt liveness **10/10**,
content-verified **10/10**. Capability C is **DEFERRED**: the compute is ECS Fargate (a container service,
shell-less), so the off-clock micro-probes (sysbench/STREAM/fio over SSH) do not apply, and the VM-to-VM
network axis is scored only for multi-VM operations. Efficiency (Part 3) floor-ratio **7.7x** [95% CI 6.0,
9.7] against the min-observed platform floor.

## The two partial-score runs (both reached a working end-state)

- **run06 (3/5).** integrate needed two fair fixes the agent found and applied: the tracking script was
  loaded over http from an https page (chromium blocked it as mixed content), and umami silently drops a
  HeadlessChrome visit as a bot. The agent served umami's `script.js` and `/api/send` over https through the
  site's own distribution and set `DISABLE_BOT_CHECK`, then proved a real headless visit records a pageview
  (a browser probe via CodeBuild, pre and post restart), enabled RDS Multi-AZ and 2 tasks so a restart never
  drops to zero, and verified login and all pageviews survived. The 3/5 reflects the checker sampling before
  those fixes landed.
- **run08 (4/5).** On the restart the agent recognized umami's Postgres was a Fargate ephemeral sidecar and
  migrated it to durable RDS; the cutover hit a Prisma-vs-RDS TLS issue that briefly took umami to zero
  tasks, which is exactly when the checker ran (probe user could not authenticate). The agent fixed the TLS
  path (`rds.force_ssl=0`), restarted, and verified the probe user logs in (200) with the migrated state
  intact.

Teardown: the reaper flagged no leftover managed database on any run and every URL is dead on re-check. The
reaper is RDS-scoped, so a full cross-resource orphan sweep across the batch is tracked separately.

Redacted, infrastructure-neutral transcripts (one resumed session per run, covering all operations and the
teardown) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0, n=10).
