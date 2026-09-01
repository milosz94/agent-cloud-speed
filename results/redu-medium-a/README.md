# redu (medium tier): umami + second site + integration + durability (2026-09-01)

n=1 (run01), a fresh run on the current harness (second-site token-naming + the universal token-scoped
teardown). It **supersedes the earlier 2026-08-31 pre-fix redu batch** (fixed-name second site,
per-service teardown), which remains in git history. n=1 is a single observation, not a distribution;
extend it by running more. redu is the adapter and validation baseline, not a paper subject.

This run **passed all five operations (5/5)**, was durable on cycle 1, content-verified (real umami app
content, 9673 bytes), and first-attempt live. The second site is **token-named** (`alcove-acsb58a8db1-...`,
the fix), and teardown left **zero resources** (verified after the run via the redu API: 0 deployments,
0 databases, 0 instances). Architecture: a fixed VM (umami) + a managed Postgres + a second VM for the site.

## Result (n=1)

| run | tier | total wall (s) | agent $ | fixed $/mo | deploy t1 (s) |
|----:|:----:|---------------:|--------:|-----------:|--------------:|
| 1 | 5/5 | 1172 | $6.79 | $44.67 | 440 |

Cost is a standing **run-rate** (a fixed VM `m1.medium` + a managed Postgres + a second VM), so it does not
scale with traffic: **$44.67/mo** at every traffic level (dated redu list price, captured 2026-09-01,
$0.0612/hr all-in).

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 440 | 33 | 209 | 266 | 225 | 1172 |

## Capability (Part 1) is MEASURED (redu is a shell-reachable VM)

Unlike a serverless surface, redu's compute is an SSH-reachable VM, so the off-clock capability micro-probes
run on the deploy's own machine. Delivered-capability index (partial, compute/memory/disk axes, normalized
against the frozen neutral-host reference): **DCI 0.23** (compute 0.067, memory 1.35, disk 0.137). The
VM-to-VM network axis is scored only for multi-VM operations.

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` = **440s** (n=1, single observation), = critical_platform **316s** + critical_agent
**119s** (overlap 0). First-attempt liveness **1/1**, content-verified **1/1**.

Redacted, infrastructure-neutral transcript (one resumed session covering all operations and the teardown)
is in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0, n=1).
