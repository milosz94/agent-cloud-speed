# aws (medium tier): umami + second site + integration + durability (2026-08-31)

n=3 (run01-03), the fresh batch through the corrected medium harness. The **Medium** tier is a five-operation
workload, not just a deploy: **deploy-serve** (umami provisions and serves), **register** (a probe admin user
exists), **deploy-site-b** (a standardized second site, a real Node app, serves), **integrate** (umami tracking
is wired into the second site and a REAL headless-browser visit records a pageview on a unique sentinel path,
0 -> 1), then a **restart-durability** goal (restart, and require every durable effect to survive). Every run
below passed all five operations, was **durable on cycle 1**, was **content-verified** (the served root is real
umami app content, 9673 bytes), and was **torn down clean** (no orphans; the URL is dead on re-check).
Architecture: serverless containers (ECS Fargate) umami + managed Postgres (RDS) + a load balancer, plus a
second Fargate site.

The known CloudFront-cache failure mode did **not** recur: on all three runs the headless visit registered a
real pageview 0 -> 1 against a fresh umami website id. Each run used a **unique** per-run load-balancer name
(`umami-acs<token>`), so no leftover deployment could answer for another run; post-teardown the URL is dead.

The overview is keyed on **total wall** (the whole workload), because a single operation's time (the deploy
`t1`) hides most of the run. The per-operation table breaks out where the time goes.

## Overview (total wall = all operations; agent $ = the whole workflow)

| run | total wall (s) | agent $ | fixed $/mo | $/mo @ 10k req | $/mo @ 500k req | $/mo @ 10M req |
|----:|---------------:|--------:|-----------:|---------------:|----------------:|---------------:|
| 1 | 3119 | $8.27 | $66.45 | $66.45 | $66.45 | $66.45 |
| 2 | 2767 | $14.80 | $48.43 | $48.43 | $48.43 | $48.43 |
| 3 | 2712 | $4.35 | $66.45 | $66.45 | $66.45 | $66.45 |

Cost is a standing **run-rate** (a Fargate task + RDS instance + load balancer run continuously), so the bill
does not scale with traffic and the three traffic columns are equal. run02 landed on a smaller/cheaper task
($48.43). Dated AWS public list price (Price List Query API), captured 2026-08-31; egress separate; some
ancillary AWS resources (listener, target-group, log-group, public-IPv4) are excluded by the cost tool.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 666 | 51 | 1013 | 810 | 579 | 3119 |
| 2 | 750 | 32 | 455 | 1050 | 480 | 2767 |
| 3 | 1120 | 39 | 379 | 566 | 608 | 2712 |

aws is the slowest platform in this suite, and it is **platform-bound**: deploy `t1` alone runs 666-1120s
(run03 is a 1120s provision outlier), and deploy-site-b is again mostly platform time from a repeated provision:

| run | deploy-site-b wall (s) | platform (s) | agent (s) |
|----:|-----------------------:|-------------:|----------:|
| 1 | 1013 | 665 | 336 |
| 2 | 455 | 96 | 343 |
| 3 | 379 | 170 | 198 |

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` (external concurrent poll, first HTTP status < 500): **845.3s** [95% CI 666.2, 1119.8],
= critical_platform **511.2s** + critical_agent **322.6s** (overlap 0). First-attempt liveness **3/3**,
content-verified **3/3**. Capability C is **DEFERRED**: the compute is ECS Fargate (a container service,
shell-less), so the off-clock micro-probes (sysbench/STREAM/fio over SSH) do not apply, and the VM-to-VM
network axis is scored only for multi-VM operations. Efficiency (Part 3) floor-ratio **2.8x** against the
min-observed platform floor.

Redacted, infrastructure-neutral transcripts (one resumed session per run, covering all operations and the
teardown) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0).
