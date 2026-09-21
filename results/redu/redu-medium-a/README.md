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

| run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | fixed $/mo | $/mo @ 10k | $/mo @ 500k | $/mo @ 10M | tier |
|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|-----------:|-----------:|------------:|-----------:|:----:|
| [1](sessions/5c8d930c-4a9a-41f9-b5da-9534545993fc.jsonl) | 1139 | 440 | 26 | 202 | 252 | 219 | $6.55 | $44.67 | $44.67 | $44.67 | $44.67 | 5/5 |
| [2](sessions/822f7785-dc35-4b19-8e07-0e7a9bbaddc5.jsonl) | 1328 | 238 | 489 | 240 | 211 | 151 | $7.02 | $44.53 | $44.53 | $44.53 | $44.53 | 5/5 |
| [3](sessions/bf0451fb-95af-4b26-984d-c5d49882ed02.jsonl) | 1374 | 208 | 14 | 289 | 383 | 480 | $5.96 | $44.53 | $44.53 | $44.53 | $44.53 | 5/5 |
| [4](sessions/2a6a8e9a-747a-4889-8bd8-7c5265382b1c.jsonl) | 2612 | 283 | 1139 | 319 | 346 | 524 | $12.38 | $44.53 | $44.53 | $44.53 | $44.53 | 5/5 |
| [5](sessions/fc1c53c8-0436-4c9c-852b-441909d7a81c.jsonl) | 1602 | 502 | 24 | 329 | 328 | 419 | $4.97 | $44.53 | $44.53 | $44.53 | $44.53 | 5/5 |
| [6](sessions/8c36ac3c-d83f-4294-a0f6-7aebe5407310.jsonl) | 956 | 278 | 20 | 207 | 238 | 212 | $5.27 | $44.53 | $44.53 | $44.53 | $44.53 | 5/5 |
| [7](sessions/91372ec4-50b5-4ca1-90d0-0e7095cfa939.jsonl) | 1294 | 244 | 16 | 360 | 364 | 310 | $5.47 | $44.53 | $44.53 | $44.53 | $44.53 | 5/5 |
| [8](sessions/079325b1-9a4a-44fe-84e4-368626c4e403.jsonl) | 2281 | 310 | 17 | 521 | 704 | 730 | $5.77 | $44.53 | $44.53 | $44.53 | $44.53 | 5/5 |
| [9](sessions/6dc203d7-7213-4101-8b3a-2509c8cf3a53.jsonl) | 2116 | 235 | 16 | 1080 | 492 | 293 | $10.05 | $44.53 | $44.53 | $44.53 | $44.53 | 5/5 |
| [10](sessions/6c88956e-0aae-4718-bfb2-f931d4e2049f.jsonl) | 1863 | 319 | 25 | 640 | 453 | 426 | $5.39 | $44.53 | $44.53 | $44.53 | $44.53 | 5/5 |

`agent $` is the **whole workflow**: deploy + the four operations + the durability cycle + teardown,
computed with that one composition for every row so the rows are comparable.

Cost is a standing **run-rate** (a fixed VM `m1.medium` + a managed Postgres + a second VM), so it does
not scale with traffic: **$44.53/mo** at every traffic level on nine runs and **$44.67/mo** on run01
(dated redu list price captured on each run's date; the small difference is the dated capture, not a
configuration change).

`register` is the widest-spread operation: 14s on run03 against 1139s on run04, an 81x range on the same
operation and the same cloud. That spread is the reason a single run is not a measurement here.

**Column basis (changed 2026-09-06).** The per-operation columns are each operation's critical-path
`split.makespan_s`, which is what the aws, gcp and azure cells publish. This cell previously published
`wall_s` for the same columns, which runs 5 to 14 seconds longer per operation because it includes the
harness verification tail. Both figures are in every run record; nothing was recomputed or re-run, and
`deploy (t1)` is unchanged because it was already time-to-serving on both bases.

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

`agent $` is the agent's LLM cost for **the task being measured**: the deploy round, plus each
operation, plus the durability cycles. Teardown is harness bookkeeping and stays out, matching the rule
`PLAYBOOK.md` states for the easy tier. The parts are disjoint time windows of one session (verified:
zero overlapping windows across the 62 medium rows whose per-run records are published here), so they sum without double counting, and the
`deploy-serve` operation carries no cost of its own on any row because the deploy round already holds it.
