# aws (medium tier, disclosed regime): umami + second site + integration + durability (2026-09-06)

| run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | fixed $/mo | $/mo @ 10k | $/mo @ 500k | $/mo @ 10M | tier |
|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|-----------:|-----------:|------------:|-----------:|:----:|
| [1](sessions/a0bf606b-edd6-4aa7-a0de-02fe0367d629.jsonl) | 5287 | 867 | 56 | 74 | 1112 | 3178 | $15.02 | $87.52 | $87.52 | $87.52 | $87.52 | 3/5 |
| [2](sessions/a1a0c504-dc73-49bb-81de-5ce9b2399055.jsonl) | 1991 | 551 | 14 | 41 | 1226 | 159 | $4.96 | $84.67 | $84.67 | $84.67 | $84.67 | 5/5 |
| [3](sessions/65712527-02d0-43eb-926f-5f692c43ed71.jsonl) | 2568 | 436 | 13 | 35 | 1780 | 304 | $5.56 | $105.89 | $105.89 | $105.89 | $105.89 | 5/5 |
| [4](sessions/5a38b428-34e3-45cf-adaa-24f108770db7.jsonl) | 2922 | 1300 | 21 | 66 | 1398 | 137 | $10.38 | $99.31 | $99.31 | $99.31 | $99.31 | 5/5 |
| [5](sessions/20ab6eb1-b9dd-46fc-a805-d77a385ed870.jsonl) | 5057 | 671 | 14 | 46 | 874 | 3452 | $19.06 | $87.52 | $87.52 | $87.52 | $87.52 | 3/5 |
| [6](sessions/89ff274c-1762-4c0e-bd51-c755cc6d76d6.jsonl) | 5754 | 596 | 51 | 27 | 4880 | 200 | $8.06 | $128.27 | $128.27 | $128.27 | $128.27 | 5/5 |
| [7](sessions/87ade837-c291-4eba-8eb2-c6a97d64f1df.jsonl) | 1581 | 1064 | 22 | 36 | 24 | 435 | $9.44 | $36.30 | $36.30 | $36.30 | $36.30 | 5/5 |
| [8](sessions/e66d9496-df9e-405d-b708-d53a4bc7b5ac.jsonl) | 1475 | 1078 | 12 | 35 | 22 | 328 | $3.75 | $37.03 | $37.03 | $37.03 | $37.03 | 5/5 |
| [9](sessions/3f73e2bc-d6e5-437d-b23e-be0fb4d23dfb.jsonl) | 1608 | 1233 | 25 | 34 | 26 | 290 | $4.27 | $35.57 | $35.57 | $35.57 | $35.57 | 5/5 |
| [10](sessions/f6f090dd-cccb-42f1-bccf-170a4f5e1c1c.jsonl) | 1523 | 1129 | 23 | 27 | 20 | 324 | $3.93 | $30.66 | $30.66 | $30.66 | $30.66 | 5/5 |
| [11](sessions/d77d3678-9493-414d-a3da-21dc0aaec50e.jsonl) | 1762 | 1392 | 11 | 26 | 21 | 312 | $2.99 | $30.66 | $30.66 | $30.66 | $30.66 | 5/5 |
| [12](sessions/b1343ebf-3466-4af3-9303-999186e4e768.jsonl) | 1114 | 870 | 23 | 68 | 28 | 125 | $3.43 | $95.66 | $95.66 | $95.66 | $95.66 | 5/5 |

`agent $` is the agent's LLM cost for **the task being measured**: the deploy round, plus each
operation, plus the durability cycles. Teardown is harness bookkeeping and stays out, matching the rule
`PLAYBOOK.md` states for the easy tier. The parts are disjoint time windows of one session (verified:
zero overlapping windows across all 81 medium rows), so they sum without double counting, and the
`deploy-serve` operation carries no cost of its own on any row because the deploy round already holds it.
