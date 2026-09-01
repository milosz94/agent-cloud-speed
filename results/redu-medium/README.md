# redu (medium tier): umami + second site + integration + durability (2026-08-31)

n=3 (run01-03), the fresh batch through the corrected medium harness (a real second site, a harness-driven
headless-browser visit, and host-side teardown). The **Medium** tier is a five-operation workload, not just a
deploy: **deploy-serve** (umami provisions and serves), **register** (a probe admin user exists),
**deploy-site-b** (a standardized second site, a real Node app, serves), **integrate** (umami tracking is wired
into the second site and a REAL headless-browser visit records a pageview on a unique sentinel path, 0 -> 1),
then a **restart-durability** goal (restart, and require every durable effect to survive). Every run below
passed all five operations, was **durable on cycle 1**, was **content-verified** (the served root is real umami
app content, 9673 bytes, not a default page), and was **torn down clean** (no orphans; the URL is dead on
re-check). Architecture: fixed VM (m1.medium) umami + managed Postgres + a second site.

The overview is keyed on **total wall** (the whole workload), because a single operation's time (the deploy
`t1`) hides most of the run. The per-operation table breaks out where the time goes.

## Overview (total wall = all operations; agent $ = the whole workflow)

| run | total wall (s) | agent $ | fixed $/mo | $/mo @ 10k req | $/mo @ 500k req | $/mo @ 10M req |
|----:|---------------:|--------:|-----------:|---------------:|----------------:|---------------:|
| 1 | 1694 | $9.05 | $44.67 | $44.67 | $44.67 | $44.67 |
| 2 | 2345 | $7.04 | $44.67 | $44.67 | $44.67 | $44.67 |
| 3 | 2537 | $10.16 | $44.67 | $44.67 | $44.67 | $44.67 |

`fixed $/mo` is the flat all-in price of the VM (m1.medium) + managed Postgres. It is a fixed VM, so the bill
stays **$44.67/mo at any traffic** (the three traffic columns are equal). Dated public list price, captured
2026-08-31; egress separate. GBP list price converted at GBP->USD 1.3539 (ECB, 2026-08-31).

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 207 | 33 | 885 | 309 | 260 | 1694 |
| 2 | 207 | 23 | 1264 | 653 | 198 | 2345 |
| 3 | 217 | 416 | 1040 | 579 | 285 | 2537 |

The deploy is fast and consistent (~210s), but **deploy-site-b dominates** the total, and that time is almost
all redu PLATFORM time from a REPEATED provision (the second app), not redu's per-deploy speed:

| run | deploy-site-b wall (s) | platform (s) | agent (s) |
|----:|-----------------------:|-------------:|----------:|
| 1 | 885 | 591 | 288 |
| 2 | 1264 | 1014 | 244 |
| 3 | 1040 | 827 | 207 |

(run03 register = 416s is an agent-side outlier, not a platform cost; it is kept, not trimmed.)

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` (external concurrent poll, first HTTP status < 500): **210.6s** [95% CI 207.2, 217.2],
= critical_platform **127.2s** + critical_agent **78.5s** (overlap 0). First-attempt liveness **3/3**,
content-verified **3/3**. Capability C (DCI, off-clock sysbench/STREAM/fio on the deploy's own VM vs a frozen
neutral reference) **0.27**; VM-to-VM private RTT ~0.53ms. Efficiency (Part 3) floor-ratio **1.7x** against the
min-observed platform floor.

Redacted, infrastructure-neutral transcripts (one resumed session per run, covering all operations and the
teardown) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0).
