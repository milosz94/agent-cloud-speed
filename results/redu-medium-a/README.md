# redu (medium tier): umami + second site + integration + durability (2026-09-04)

n=2 (run01 2026-09-01, run02 2026-09-03), both on the current harness (second-site token-naming + the
universal token-scoped teardown). They **supersede the earlier 2026-08-31 pre-fix redu batch**
(fixed-name second site, per-service teardown), which remains in git history. n=2 is two observations,
not a distribution; the interval below is reported only because the tool computes one. redu is the
adapter and validation baseline, **not a paper subject**.

Both runs **passed all five operations (5/5)**, were durable on cycle 1, content-verified (real umami app
content, 9673 bytes), and first-attempt live (2/2). Both second sites are token-named
(`alcove-acsb58a8db1-...`, `alcove-acsc2d0c833-...`), and teardown left **zero resources** (verified after
the batch via the redu API: 0 deployments, 0 databases, 0 instances, 0 volumes, quota all-zero).
Architecture: a fixed VM (umami) + a managed Postgres + a second VM for the site.

Both runs stopped the clock on a real **HTTP 200**, so neither is affected by the platform-edge 4xx
defect found on other adapters 2026-09-03 (see the repo root and the paper's RESULTS-DATA).

## Result (n=2)

| run | tier | total wall (s) | agent $ | fixed $/mo | deploy t1 (s) |
|----:|:----:|---------------:|--------:|-----------:|--------------:|
| 1 | 5/5 | 1172 | $8.96 | $44.67 | 440 |
| 2 | 5/5 | 1361 | $9.87 | $44.53 | 238 |

`agent $` is the **whole workflow**: deploy + the four operations + the durability cycle + teardown,
recomputed for both rows on 2026-09-04 with that one composition so the two are comparable (the earlier
n=1 table quoted a narrower figure).

Cost is a standing **run-rate** (a fixed VM `m1.medium` + a managed Postgres + a second VM), so it does
not scale with traffic: **$44.67/mo** and **$44.53/mo** at every traffic level (dated redu list price,
captured on each run's date; the small difference is the dated capture, not a configuration change).

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 440 | 33 | 209 | 266 | 225 | 1172 |
| 2 | 238 | 496 | 246 | 224 | 157 | 1361 |

The two runs split their time differently: run01 spent it in the first deploy (440s) with a fast
register (33s), run02 served in 238s but spent 496s on register. With n=2 that is variance, not a
pattern.

## Capability (Part 1) is MEASURED (redu is a shell-reachable VM)

Unlike a serverless surface, redu's compute is an SSH-reachable VM, so the off-clock capability
micro-probes run on the deploy's own machine. Delivered-capability index (compute/memory/disk axes,
normalized against the frozen neutral-host reference): **DCI 0.232 and 0.246**.

The **VM-to-VM network axis (C17) is measured here**, because the medium tier is a genuinely multi-VM
operation: private-network RTT **0.455 ms** and **0.558 ms**. This is the only cell in the tree with the
network axis populated; on the shell-less serverless surfaces it is correctly N/A.

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` mean **338.8s** [95% CI 237.5, 440.1] (n=2), = critical_platform **228.2s**
[140.4, 316.0] + critical_agent **105.6s** [91.8, 119.4], overlap 0. First-attempt liveness **2/2**,
content-verified **2/2**.

Efficiency (Part 3): floor-ratio **2.4x** [95% CI 1.7, 3.1] against F_C = 140.4s (the min observed
critical-platform floor over n=2, so it is a two-sample minimum and will drift as the pool grows);
bracket [140.4, 232.1]s. Selection excess is 0 by construction (one-operation suite).

Redacted, infrastructure-neutral transcripts (one resumed session per run, covering all operations and
the teardown) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0, n=2).
