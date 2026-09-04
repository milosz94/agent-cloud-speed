# redu (medium tier): umami + second site + integration + durability (2026-09-04)

n=10 (run01 2026-09-01, run02 2026-09-03, run03 to run10 2026-09-04), all on the current harness
(second-site token-naming + the universal token-scoped teardown). They **supersede the earlier
2026-08-31 pre-fix redu batch** (fixed-name second site, per-service teardown), which remains in git
history. redu is the adapter and validation baseline, **not a paper subject**.

All ten runs **passed all five operations (5/5)**, were content-verified (real umami app content) and
first-attempt live (10/10). Architecture on every run: a fixed VM (umami) + a managed Postgres + a
second VM for the site. Teardown left **zero resources** (verified after the batch via the redu API:
0 instances, 0 volumes, 0 databases, 0 snapshots, 0 clusters).

Every run stopped the clock on a real **HTTP 200**, so none is affected by the platform-edge 4xx defect
found on other adapters 2026-09-03, and none is excluded by the first-poll rule in gold 1.1.0.

## Result (n=10)

| run | tier | total wall (s) | agent $ | fixed $/mo | deploy t1 (s) |
|----:|:----:|---------------:|--------:|-----------:|--------------:|
| 1 | 5/5 | 1259 | $7.88 | $44.67 | 440 |
| 2 | 5/5 | 1456 | $9.00 | $44.53 | 238 |
| 3 | 5/5 | 1507 | $6.88 | $44.53 | 208 |
| 4 | 5/5 | 2747 | $12.74 | $44.53 | 283 |
| 5 | 5/5 | 1721 | $6.10 | $44.53 | 502 |
| 6 | 5/5 | 1098 | $6.09 | $44.53 | 278 |
| 7 | 5/5 | 1425 | $6.60 | $44.53 | 244 |
| 8 | 5/5 | 2439 | $7.10 | $44.53 | 310 |
| 9 | 5/5 | 2258 | $10.57 | $44.53 | 235 |
| 10 | 5/5 | 1979 | $6.08 | $44.53 | 319 |

`agent $` is the **whole workflow**: deploy + the four operations + the durability cycle + teardown,
computed with that one composition for every row so the rows are comparable.

Cost is a standing **run-rate** (a fixed VM `m1.medium` + a managed Postgres + a second VM), so it does
not scale with traffic: **$44.53/mo** at every traffic level on nine runs and **$44.67/mo** on run01
(dated redu list price captured on each run's date; the small difference is the dated capture, not a
configuration change).

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 440 | 33 | 209 | 266 | 225 | 1259 |
| 2 | 238 | 496 | 246 | 224 | 157 | 1456 |
| 3 | 208 | 20 | 294 | 396 | 486 | 1507 |
| 4 | 283 | 1146 | 324 | 358 | 530 | 2747 |
| 5 | 502 | 30 | 335 | 340 | 424 | 1721 |
| 6 | 278 | 25 | 212 | 250 | 218 | 1098 |
| 7 | 244 | 22 | 365 | 376 | 316 | 1425 |
| 8 | 310 | 23 | 526 | 716 | 736 | 2439 |
| 9 | 235 | 22 | 1086 | 504 | 298 | 2258 |
| 10 | 319 | 31 | 645 | 464 | 432 | 1979 |

`register` is the widest-spread operation: 20s on run03 against 1146s on run04, a 57x range on the same
operation and the same cloud. That spread is the reason a single run is not a measurement here.

## Capability (Part 1) is MEASURED, but only on the first two runs

redu's compute is an SSH-reachable VM, so the off-clock capability micro-probes can run on the deploy's
own machine. Delivered-capability index (compute/memory/disk axes, normalized against the frozen
neutral-host reference): **DCI 0.232 and 0.246** (n=2).

The **VM-to-VM network axis (C17)** is measured on those same two runs, because the medium tier is a
genuinely multi-VM operation: private-network RTT **0.455 ms** and **0.558 ms**. This remains the only
cell in the tree with the network axis populated; on the shell-less serverless surfaces it is correctly
N/A.

**Disclosed gap:** on run03 to run10 the capability probe reported `no SSH within 180s` against the
keypairs it holds, so C is absent on those eight runs. It is an off-clock, non-fatal probe, so the
timing rows above are unaffected, but the capability axis here is n=2, not n=10.

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` mean **305.6s** [95% CI 254.2, 364.5] (n=10), = critical_platform **204.2s**
[155.7, 259.1] + critical_agent **97.0s** [91.0, 104.0], overlap 0. First-attempt liveness **10/10**,
content-verified **10/10**.

Efficiency (Part 3): floor-ratio **2.4x** [95% CI 2.0, 2.9] against F_C = 123.7s (the min observed
critical-platform floor over n=10); bracket [123.7, 203.3]s = 1.643x wide. Floor sensitivity: the
bracket ratio moves 1.494 to 1.826 at F_C(1 +/- 0.1). Selection excess is 0 by construction
(one-operation provision gold). Gold `provision-deploy/1.1.0`.

Redacted, infrastructure-neutral transcripts (one resumed session per run, covering all operations and
the teardown) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0).
