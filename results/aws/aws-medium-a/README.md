# aws: Medium-tier (online) umami (2026-09-01)

| run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | fixed $/mo | $/mo @ 10k | $/mo @ 500k | $/mo @ 10M | tier |
|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|-----------:|-----------:|------------:|-----------:|:----:|
| [1](sessions/a0c27b89-6125-4308-821f-95d80c7e31f5.jsonl) | 3060 | 666 | 38 | 1001 | 788 | 567 | $6.93 | $77.40 | $77.40 | $77.40 | $77.40 | 5/5 |
| [2](sessions/41a44d06-8086-4a00-8f01-8618c43eaaae.jsonl) | 2700 | 750 | 19 | 439 | 1025 | 467 | $11.16 | $72.04 | $72.04 | $72.04 | $72.04 | 5/5 |
| [3](sessions/3aa325ac-b8ed-4eaa-a127-fdb852c8c8d6.jsonl) | 2658 | 1120 | 27 | 368 | 546 | 597 | $3.88 | $81.05 | $81.05 | $81.05 | $81.05 | 5/5 |
| [4](sessions/0c988acb-dd17-472e-a6b0-cccebb6f1843.jsonl) | 3803 | 759 | 37 | 505 | 1632 | 870 | $6.88 | $81.05 | $81.05 | $81.05 | $81.05 | 5/5 |
| [5](sessions/9946d834-3ebe-4a99-8ee4-883cb1e992e5.jsonl) | 3640 | 1664 | 37 | 407 | 610 | 922 | $8.29 | $77.40 | $77.40 | $77.40 | $77.40 | 5/5 |
| [6](sessions/432b39d8-faa8-4d01-8f0b-bb51066e88e8.jsonl) | 7014 | 850 | 26 | 906 | 1674 | 3558 | $13.07 | $77.41 | $77.41 | $77.41 | $77.41 | 3/5 |
| [7](sessions/1eb9e227-f075-47dc-bb3e-8ab1b44870ac.jsonl) | 4127 | 276 | 21 | 498 | 1154 | 2178 | $8.55 | $95.42 | $95.42 | $95.42 | $95.42 | 4/5 |
| [8](sessions/2f764f9a-161a-4d4d-85b0-4ae02aa9b2e9.jsonl) | 4788 | 738 | 30 | 476 | 2650 | 894 | $6.01 | $66.25 | $66.25 | $66.25 | $66.25 | 5/5 |
| [9](sessions/4aecb882-3d7c-4c9a-ae1a-1526d5960e03.jsonl) | 6284 | 237 | 34 | 411 | 2623 | 2979 | $12.16 | $83.39 | $83.39 | $83.39 | $83.39 | 5/5 |
| [10](sessions/d855e6ff-b45c-4443-9dd7-3af008effe48.jsonl) | 3273 | 731 | 18 | 525 | 1191 | 808 | $5.45 | $87.92 | $87.92 | $87.92 | $87.92 | 5/5 |

Run numbers are the table's own 1 to 10; each row's session file is its durable identifier. Three
attempts from these batches are not published: their standing EC2 was unpriced, so they could not enter
the cost axis (`DATA-DEFECTS.md`).

Every cost figure in this cell was re-priced on 2026-09-07 from the settled CloudTrail log, so all ten
rows now come from one mechanism (see `../../REPRICE-2026-09-07.md`). Row 9's missing database, open
until then, is resolved: one WAS standing and the live snapshot had missed it.

`agent $` is the agent's LLM cost for **the task being measured**: the deploy round, plus each
operation, plus the durability cycles. Teardown is harness bookkeeping and stays out, matching the rule
`PLAYBOOK.md` states for the easy tier. The parts are disjoint time windows of one session (verified:
zero overlapping windows across all 81 medium rows), so they sum without double counting, and the
`deploy-serve` operation carries no cost of its own on any row because the deploy round already holds it.
