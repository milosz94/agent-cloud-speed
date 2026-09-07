# gcp: Medium-tier (online) umami (2026-09-04)

| run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | fixed $/mo | $/mo @ 10k | $/mo @ 500k | $/mo @ 10M | tier |
|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|-----------:|-----------:|------------:|-----------:|:----:|
| [1](sessions/97ec285a-4f45-4f9d-b236-e9c54a6fc453.jsonl) | 2428 | 472 | 42 | 368 | 1103 | 443 | $4.79 | - | $29.28 | $30.72 | $58.51 | 5/5 |
| [2](sessions/23e283ed-49b6-45a3-a9ee-3410effdd1da.jsonl) | 1113 | 365 | 27 | 201 | 164 | 356 | $3.72 | - | $47.17 | $48.60 | $76.39 | 5/5 |
| [3](sessions/79bf64f7-f357-4f00-ad9f-c34e41b4f4c2.jsonl) | 1326 | 446 | 31 | 171 | 172 | 506 | $3.96 | - | $29.28 | $30.72 | $58.51 | 5/5 |
| [4](sessions/6d3821e6-4376-4ac0-bd78-422b24ffdc36.jsonl) | 1387 | 461 | 40 | 300 | 184 | 402 | $4.50 | - | $29.28 | $30.72 | $58.51 | 5/5 |
| [5](sessions/16493664-998a-46f5-80d1-b819e8bc792c.jsonl) | 1878 | 469 | 16 | 195 | 693 | 505 | $5.62 | - | $42.42 | $43.86 | $71.64 | 5/5 |
| [6](sessions/b7762fc0-3a8d-48b0-8627-80ba8764866a.jsonl) | 1280 | 445 | 34 | 236 | 177 | 388 | $3.28 | - | $29.28 | $30.72 | $58.51 | 5/5 |
| [7](sessions/2e771768-ff6f-47de-86cd-de8130476bc0.jsonl) | 1751 | 481 | 34 | 202 | 866 | 168 | $4.91 | - | $68.67 | $68.67 | $68.67 | 5/5 |
| [8](sessions/be950c75-9035-467b-b501-9a99c6975f99.jsonl) | 1786 | 485 | 34 | 215 | 667 | 385 | $5.07 | - | $29.28 | $30.72 | $58.51 | 5/5 |
| [9](sessions/c14d2415-c14d-4b80-b46f-46a38d918be3.jsonl) | 1031 | 479 | 40 | 185 | 169 | 158 | $2.68 | - | $29.28 | $30.72 | $58.51 | 5/5 |
| [10](sessions/08465ab0-b4d4-4989-8595-76bdb545871e.jsonl) | 4290 | 463 | 46 | 206 | 952 | 2623 | $9.31 | - | $68.67 | $68.67 | $68.67 | 4/5 |

`agent $` is the agent's LLM cost for **the task being measured**: the deploy round, plus each
operation, plus the durability cycles. Teardown is harness bookkeeping and stays out, matching the rule
`PLAYBOOK.md` states for the easy tier. The parts are disjoint time windows of one session (verified:
zero overlapping windows across all 81 medium rows), so they sum without double counting, and the
`deploy-serve` operation carries no cost of its own on any row because the deploy round already holds it.
