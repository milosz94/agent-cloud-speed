# azure (medium tier, disclosed regime): umami + second site + integration + durability (2026-09-05)

| run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | fixed $/mo | $/mo @ 10k | $/mo @ 500k | $/mo @ 10M | tier |
|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|-----------:|-----------:|------------:|-----------:|:----:|
| [1](sessions/4e6d51a7-e9bb-43fb-bc6d-4e014a96c16d.jsonl) | 1792 | 1370 | 23 | 38 | 27 | 334 | $4.35 | $37.23 | $37.23 | $37.23 | $37.23 | 5/5 |
| [2](sessions/ac400b33-9458-4253-bc89-12795570e2ba.jsonl) | 1249 | 1046 | 12 | 43 | 22 | 126 | $2.53 | - | $45.94 | $47.38 | $75.41 | 5/5 |
| [4](sessions/ca89af8f-f311-42a5-9187-46d9487971a0.jsonl) | 1770 | 1468 | 34 | 20 | 30 | 218 | $6.15 | $39.57 | $39.57 | $39.57 | $39.57 | 5/5 |
| [5](sessions/432c2ee4-af12-4663-adfd-f9ac0abe1f97.jsonl) | 1394 | 886 | 25 | 33 | 30 | 420 | $5.18 | $37.23 | $37.23 | $37.23 | $37.23 | 5/5 |
| [6](sessions/aef838ba-e144-4389-944c-1a6748656441.jsonl) | 2663 | 2341 | 10 | 29 | 23 | 260 | $2.66 | - | $44.84 | $46.28 | $74.31 | 5/5 |
| [7](sessions/98719f54-9d16-4aa2-8d3e-bcb388f2d84c.jsonl) | 1787 | 1546 | 35 | 15 | 18 | 173 | $5.27 | $37.23 | $37.23 | $37.23 | $37.23 | 5/5 |
| [8](sessions/dc28d2fb-fdf0-4fe4-8ab0-53fb52a46800.jsonl) | 2043 | 1807 | 11 | 12 | 20 | 193 | $4.57 | - | $44.84 | $46.28 | $74.31 | 5/5 |
| [9](sessions/4a7318ab-90b2-412c-b1e6-afb9e151b1d6.jsonl) | 1688 | 1428 | 13 | 30 | 22 | 195 | $6.00 | $37.23 | $37.23 | $37.23 | $37.23 | 5/5 |
| [10](sessions/a36f967f-b3ad-4162-9d8f-9391e3470ecd.jsonl) | 1791 | 1477 | 12 | 96 | 31 | 175 | $4.00 | $37.23 | $37.23 | $37.23 | $37.23 | 5/5 |

**Run 3 was removed on 2026-09-07 and will be re-run.** Its record carried `"split": null`, so it had
no `critical_platform_s` and was already absent from the Part 3 floor ratio (which is why that row
read n=9 against a 10-row table). It reached the goal predicate and scored 5/5, but the split could
not be recomputed from what was stored, so the run was withdrawn rather than published with a hole.
Run ids are unchanged, so the gap at 3 is where the re-run lands.
