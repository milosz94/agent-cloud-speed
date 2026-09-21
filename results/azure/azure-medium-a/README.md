# azure: Medium-tier (online) umami (2026-09-01)

| run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | fixed $/mo | $/mo @ 10k | $/mo @ 500k | $/mo @ 10M | tier |
|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|-----------:|-----------:|------------:|-----------:|:----:|
| [1](sessions/b62c17d1-8d4e-48ad-8b8a-1c22c34d0107.jsonl) | 2179 | 1221 | 22 | 346 | 61 | 529 | $4.32 | - | $40.87 | $42.31 | $70.34 | 5/5 |
| [2](sessions/9bb69ed8-b98a-4dfa-a529-131e716b3df6.jsonl) | 2827 | 2252 | 32 | 194 | 152 | 197 | $5.12 | - | $40.87 | $42.31 | $70.34 | 5/5 |
| [3](sessions/6ebe9f00-16d9-4e5f-8635-54501196d1c1.jsonl) | 3083 | 2201 | 35 | 446 | 142 | 259 | $5.21 | - | $41.86 | $43.31 | $71.34 | 5/5 |
| [4](sessions/d544437e-6806-405c-a559-5bf16936ea5f.jsonl) | 1916 | 1350 | 32 | 308 | 58 | 168 | $5.08 | - | $39.77 | $41.22 | $69.24 | 5/5 |
| [5](sessions/fc476b4a-62bf-46ca-a7d5-a515c1d6c311.jsonl) | 2788 | 1996 | 30 | 292 | 135 | 335 | $5.55 | - | $39.77 | $41.22 | $69.24 | 5/5 |
| [6](sessions/41605c56-e2c8-4fa8-9eae-e646326a28fd.jsonl) | 3584 | 2521 | 297 | 365 | 181 | 220 | $6.11 | - | $39.77 | $41.22 | $69.24 | 5/5 |
| [7](sessions/bef9e3ac-b326-4c68-9755-2b6ae52c0238.jsonl) | 2914 | 1503 | 530 | 339 | 203 | 339 | $8.47 | $24.82 | $24.82 | $24.82 | $24.82 | 5/5 |
| [8](sessions/f7c1868d-5cc5-4d0c-86a8-36b79253c21f.jsonl) | 2427 | 1521 | 49 | 452 | 166 | 239 | $5.51 | - | $39.77 | $41.22 | $69.24 | 5/5 |
| [9](sessions/8af17e04-14aa-4732-bf68-dc67a798b8f9.jsonl) | 1315 | 740 | 48 | 251 | 134 | 142 | $10.23 | - | $39.77 | $41.22 | $69.24 | 5/5 |
| [10](sessions/a29d63d5-dcc7-4065-934a-abec2b3647ca.jsonl) | 2879 | 2201 | 48 | 248 | 168 | 214 | $5.58 | - | $40.87 | $42.31 | $70.34 | 5/5 |

`fixed $/mo` is a standing run-rate and exists only where the deployment has one. Runs 1-6 and 8-9
landed on Azure Container Apps, which is usage-metered, so there is no fixed monthly figure for them
and the three traffic columns carry the whole cost. Run 7 landed on an App Service plan (B1) plus a
flexible Postgres, which is a standing rate, so it is flat at $24.82/mo across every traffic level.

Cost is per run, from that run's own record. The three traffic columns previously repeated run 1's
estimate on every row; they now carry each run's measured figure.

Run numbers are the table's own 1 to 10; each row's session file is its durable identifier.

**Three attempts were withdrawn on 2026-09-07, and row 10 is the replacement.** All three carried
`"split": null`, so they had no `critical_platform_s` and were already absent from the Part 3 floor
ratio. Each reached its goal predicate, but the split could not be recomputed from what was stored.
The cause was found and closed the same day: the harness ran agent turns on the HOST when the microVM
substrate was unavailable, warning rather than refusing, so every quantity read from the VM came back
null while `time_to_serving_s` survived and made the run look successful. A second attempt failed a
different way, deploying the acspeed checkout itself and overflowing the guest disk. `autorun.py` now
refuses in all three cases before any cloud resource exists.

`agent $` is the agent's LLM cost for **the task being measured**: the deploy round, plus each
operation, plus the durability cycles. Teardown is harness bookkeeping and stays out, matching the rule
`PLAYBOOK.md` states for the easy tier. The parts are disjoint time windows of one session (verified:
zero overlapping windows across the 62 medium rows whose per-run records are published here), so they sum without double counting, and the
`deploy-serve` operation carries no cost of its own on any row because the deploy round already holds it.
