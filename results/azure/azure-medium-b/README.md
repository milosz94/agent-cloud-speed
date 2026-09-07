# azure (medium tier, disclosed regime): umami + second site + integration + durability (2026-09-05)

| run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | fixed $/mo | $/mo @ 10k | $/mo @ 500k | $/mo @ 10M | tier |
|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|-----------:|-----------:|------------:|-----------:|:----:|
| [1](sessions/4e6d51a7-e9bb-43fb-bc6d-4e014a96c16d.jsonl) | 1792 | 1370 | 23 | 38 | 27 | 334 | $4.33 | $37.23 | $37.23 | $37.23 | $37.23 | 5/5 |
| [2](sessions/ac400b33-9458-4253-bc89-12795570e2ba.jsonl) | 1249 | 1046 | 12 | 43 | 22 | 126 | $3.76 | - | $45.94 | $47.38 | $75.41 | 5/5 |
| [3](sessions/ca89af8f-f311-42a5-9187-46d9487971a0.jsonl) | 1770 | 1468 | 34 | 20 | 30 | 218 | $6.21 | $39.57 | $39.57 | $39.57 | $39.57 | 5/5 |
| [4](sessions/432c2ee4-af12-4663-adfd-f9ac0abe1f97.jsonl) | 1394 | 886 | 25 | 33 | 30 | 420 | $5.12 | $37.23 | $37.23 | $37.23 | $37.23 | 5/5 |
| [5](sessions/aef838ba-e144-4389-944c-1a6748656441.jsonl) | 2663 | 2341 | 10 | 29 | 23 | 260 | $3.68 | - | $44.84 | $46.28 | $74.31 | 5/5 |
| [6](sessions/98719f54-9d16-4aa2-8d3e-bcb388f2d84c.jsonl) | 1787 | 1546 | 35 | 15 | 18 | 173 | $4.83 | $37.23 | $37.23 | $37.23 | $37.23 | 5/5 |
| [7](sessions/dc28d2fb-fdf0-4fe4-8ab0-53fb52a46800.jsonl) | 2043 | 1807 | 11 | 12 | 20 | 193 | $5.68 | - | $44.84 | $46.28 | $74.31 | 5/5 |
| [8](sessions/4a7318ab-90b2-412c-b1e6-afb9e151b1d6.jsonl) | 1688 | 1428 | 13 | 30 | 22 | 195 | $6.34 | $37.23 | $37.23 | $37.23 | $37.23 | 5/5 |
| [9](sessions/a36f967f-b3ad-4162-9d8f-9391e3470ecd.jsonl) | 1791 | 1477 | 12 | 96 | 31 | 175 | $3.81 | $37.23 | $37.23 | $37.23 | $37.23 | 5/5 |
| [10](sessions/b3a253e8-873c-47d4-8468-79eddb206ff2.jsonl) | 2299 | 1999 | 14 | 51 | 25 | 210 | $5.41 | $40.81 | $40.81 | $40.81 | $40.81 | 5/5 |

Run numbers are the table's own 1 to 10; each row's session file is its durable identifier. Two attempts
from these batches are not published, and both are excluded on a stated criterion rather than on outcome:
one ended `FAILURE-no-url` with no served URL, failing the first fairness criterion; one carried
`"split": null`, so it had no `critical_platform_s` and could not enter the Part 3 floor ratio, and the
split could not be recomputed from what was stored.

`agent $` is the agent's LLM cost for **the task being measured**: the deploy round, plus each
operation, plus the durability cycles. Teardown is harness bookkeeping and stays out, matching the rule
`PLAYBOOK.md` states for the easy tier. The parts are disjoint time windows of one session (verified:
zero overlapping windows across all 81 medium rows), so they sum without double counting, and the
`deploy-serve` operation carries no cost of its own on any row because the deploy round already holds it.
