# aws: Medium-tier (online) umami (2026-09-01)

| run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | fixed $/mo | $/mo @ 10k | $/mo @ 500k | $/mo @ 10M | tier |
|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|-----------:|-----------:|------------:|-----------:|:----:|
| [1](sessions/a0c27b89-6125-4308-821f-95d80c7e31f5.jsonl) | 3060 | 666 | 38 | 1001 | 788 | 567 | $8.27 | $66.45 | $66.45 | $66.45 | $66.45 | 5/5 |
| [2](sessions/41a44d06-8086-4a00-8f01-8618c43eaaae.jsonl) | 2700 | 750 | 19 | 439 | 1025 | 467 | $14.80 | $48.43 | $48.43 | $48.43 | $48.43 | 5/5 |
| [3](sessions/3aa325ac-b8ed-4eaa-a127-fdb852c8c8d6.jsonl) | 2658 | 1120 | 27 | 368 | 546 | 597 | $4.35 | $66.45 | $66.45 | $66.45 | $66.45 | 5/5 |
| [4](sessions/0c988acb-dd17-472e-a6b0-cccebb6f1843.jsonl) | 3803 | 759 | 37 | 505 | 1632 | 870 | $8.51 | $66.45 | $66.45 | $66.45 | $66.45 | 5/5 |
| [5](sessions/9946d834-3ebe-4a99-8ee4-883cb1e992e5.jsonl) | 3640 | 1664 | 37 | 407 | 610 | 922 | $8.49 | $48.43 | $48.43 | $48.43 | $48.43 | 5/5 |
| [6](sessions/432b39d8-faa8-4d01-8f0b-bb51066e88e8.jsonl) | 7014 | 850 | 26 | 906 | 1674 | 3558 | $15.39 | $66.45 | $66.45 | $66.45 | $66.45 | 3/5 |
| [7](sessions/1eb9e227-f075-47dc-bb3e-8ab1b44870ac.jsonl) | 4127 | 276 | 21 | 498 | 1154 | 2178 | $12.01 | $52.47 | $52.47 | $52.47 | $52.47 | 4/5 |
| [8](sessions/2f764f9a-161a-4d4d-85b0-4ae02aa9b2e9.jsonl) | 4788 | 738 | 30 | 476 | 2650 | 894 | $7.63 | $113.44 | $113.44 | $113.44 | $113.44 | 5/5 |
| [9](sessions/4aecb882-3d7c-4c9a-ae1a-1526d5960e03.jsonl) | 6284 | 237 | 34 | 411 | 2623 | 2979 | $15.37 | $77.61 | $77.61 | $77.61 | $77.61 | 5/5 |
| [10](sessions/d855e6ff-b45c-4443-9dd7-3af008effe48.jsonl) | 3273 | 731 | 18 | 525 | 1191 | 808 | $6.29 | $117.09 | $117.09 | $117.09 | $117.09 | 5/5 |

Run numbers are the table's own 1 to 10; each row's session file is its durable identifier. Three
attempts from these batches are not published: their standing EC2 was unpriced, so they could not enter
the cost axis (`DATA-DEFECTS.md`).

**The cost columns of rows 8 to 10 are not comparable to rows 1 to 7.** Rows 1 to 7 were priced by the
AWS Price List Query API, which excluded public IPv4 addresses and counted one container of N. Rows 8
to 10 were priced by the newer mechanism (published bulk offer index, resources discovered from
CloudTrail management events), which prices both. The newer figure is the more complete one; the gap is
a pricer difference, not a cloud difference, and the earlier rows are the ones that need re-pricing.
The time columns and the tier score are unaffected: they are measured wall-clock and are comparable
across every row.

Row 9 carries no priced database while its transcript shows both `CreateDBInstance` and
`DeleteDBInstance` calls. Re-reading the settled CloudTrail log on 2026-09-07 shows a database WAS
standing and the live snapshot missed it (`DATA-DEFECTS.md` 13), so this row's cost is an under-count.
It is published as measured, with the correction recorded rather than silently applied.
