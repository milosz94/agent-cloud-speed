# AWS (medium tier, disclosed regime): umami + second site + integration + durability (2026-09-05)

| run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | $/mo @ 10k | $/mo @ 500k | $/mo @ 10M | tier |
|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|-----------:|------------:|-----------:|:----:|
| [3](sessions/a0bf606b-edd6-4aa7-a0de-02fe0367d629.jsonl) | 5287 | 867 | 56 | 74 | 1112 | 3178 | $6.60 | $65.62 | $65.62 | $65.62 | 3/5 |
| [5](sessions/a1a0c504-dc73-49bb-81de-5ce9b2399055.jsonl) | 1991 | 551 | 14 | 41 | 1226 | 159 | $1.86 | $66.42 | $66.42 | $66.42 | 5/5 |
| [6](sessions/65712527-02d0-43eb-926f-5f692c43ed71.jsonl) | 2568 | 436 | 13 | 35 | 1780 | 304 | $3.76 | $68.02 | $68.02 | $68.02 | 5/5 |
| [8](sessions/5a38b428-34e3-45cf-adaa-24f108770db7.jsonl) | 2922 | 1300 | 21 | 66 | 1398 | 137 | $7.75 | $81.06 | $81.06 | $81.06 | 5/5 |
| [13](sessions/20ab6eb1-b9dd-46fc-a805-d77a385ed870.jsonl) | 5057 | 671 | 14 | 46 | 874 | 3452 | $7.48 | $65.62 | $65.62 | $65.62 | 3/5 |
| [15](sessions/3eaf6819-7aad-4e52-9c0b-ba330f9e3542.jsonl) | 9195 | 486 | 23 | 73 | 1415 | 7198 | $6.66 | $66.42 | $66.42 | $66.42 | 3/5 |
| [16](sessions/89ff274c-1762-4c0e-bd51-c755cc6d76d6.jsonl) | 5754 | 596 | 51 | 27 | 4880 | 200 | $5.48 | $66.42 | $66.42 | $66.42 | 5/5 |
| [18](sessions/34c7e294-d3a9-4228-8cbd-52b332c071b3.jsonl) | 5959 | 507 | 10 | 35 | 1450 | 3957 | $8.73 | $100.89 | $100.89 | $100.89 | 3/5 |
| [22](sessions/87ade837-c291-4eba-8eb2-c6a97d64f1df.jsonl) | 1581 | 1064 | 22 | 36 | 24 | 435 | $9.06 | $36.30 | $36.30 | $36.30 | 5/5 |
| [23](sessions/e66d9496-df9e-405d-b708-d53a4bc7b5ac.jsonl) | 1475 | 1078 | 12 | 35 | 22 | 328 | $4.27 | $37.03 | $37.03 | $37.03 | 5/5 |
| [24](sessions/3f73e2bc-d6e5-437d-b23e-be0fb4d23dfb.jsonl) | 1608 | 1233 | 25 | 34 | 26 | 290 | $5.02 | $35.57 | $35.57 | $35.57 | 5/5 |
| [25](sessions/f6f090dd-cccb-42f1-bccf-170a4f5e1c1c.jsonl) | 1523 | 1129 | 23 | 27 | 20 | 324 | $4.56 | $30.66 | $30.66 | $30.66 | 5/5 |
| [26](sessions/d77d3678-9493-414d-a3da-21dc0aaec50e.jsonl) | 1762 | 1392 | 11 | 26 | 21 | 312 | $3.35 | $30.66 | $30.66 | $30.66 | 5/5 |
| [27](sessions/b1343ebf-3466-4af3-9303-999186e4e768.jsonl) | 1114 | 870 | 23 | 68 | 28 | 125 | $2.22 | $95.66 | $95.66 | $95.66 | 5/5 |

n = 14 fair of 27 attempted; 10 of the 14 reached the goal predicate (5, 6, 8, 16, 22, 23, 24, 25, 26,
27). Every column is a critical-path second and the per-operation columns sum to the total (Part 4's M).
`agent $` is the deploy round plus the deprovision turn only; it excludes the agent spend on the four
scored operations, which for run 13 is a further $11.58.

Runs 22 to 27 are the first six measured after the cost path was rebuilt (2026-09-06). Every earlier row
in this table was priced by the enumerate-then-price adapter; these six were priced from CloudTrail
management events joined to the published price list by usagetype. The six reach the goal predicate 6/6
against 4/8 for the rows above them, which is a change in the runs and not in the instrument: the tier
score is measured off-clock by the runner and is untouched by the cost work.

## What the `$/mo` column actually prices, on every row

The run-rate is snapshotted **immediately after the deploy operation and before the suite runs**, by
design: `autorun.py` step 6d prices "the un-perturbed deployment", step 6e then drives the tier. So the
figure is the **deploy bundle's** standing rate, not the whole medium deployment's. Anything the suite
provisions afterwards, site B above all, is never in it. That holds for every row here and for every row
in all six medium cells; it is a property of the measurement, not of these runs. Read the column as
"deploy-leg run-rate" and nothing wider.

## Exclusions, and why

Thirteen runs are attempted but not published. They are real data; each would misrepresent the cloud.

| runs | reason |
|---|---|
| 1, 2, 4, 7 | the Lightsail container hostname answered 404 on the first poll, so t1 measured when the hostname appeared, not time-to-serving (edge-4xx contamination, fixed in `04a40ad`) |
| 9, 10 | two Lightsail container services served and the run-rate priced one |
| 11, 12 | one or more Application Load Balancers built and never priced (the tag-independent enumerators were dead; fixed in `64d1fa6`) |
| 14 | a co-provisioned RDS instance built and never priced (same fix) |
| **17** | **its datastore existed before the deploy finished and was not priced: the container ran in `us-east-2` while its RDS was `umami-db-acsb845f10e.<...>.us-east-1.rds.amazonaws.com`, and backend enumeration only ever asked the container's own region. Priced $10.00/mo against this cell's $65 to $101 band** |
| **19** | **same shape, different mechanism: its datastore was a Lightsail RELATIONAL DATABASE, and the merge dropped every `service == "lightsail"` row on the theory that the live path prices it. The live path prices container services only, so it fell through both. Priced $15.00/mo** |
| 20 | cancelled part-way; its teardown never completed, so the record describes a run that did not finish |
| 21 | its Lightsail relational database was not priced, the same shape as 19. Priced $22.00/mo against this cell's band |

For 17 and 19 the datastore was verified to pre-date the cost snapshot from the run's own transcript
timestamps (first database call at 06:05:08Z and 09:22:35Z against deploy-serve verified at 06:44:11Z and
09:48:15Z), so it existed when the run-rate was taken and should have been in it.

**Run 15 was excluded on 2026-09-05 and has been reinstated.** Its RDS was created *after* the deploy
finished (first database call 02:08:32Z against deploy-serve verified 01:21:10Z), so it post-dates the
cost snapshot exactly as site B does on every other row. Its cost block is correct for what the column
measures, and the original exclusion was an error by the reviewer, not a defect in the run.

Both real defects are fixed in `acspeed/adapters/aws_runrate.py`: enumeration now sweeps the candidate
regions and deduplicates by ARN, and only the Lightsail *container service* is dropped from the merge,
with anything else Lightsail priced or else surfaced in `unpriced_resources` so the fairness filter
rejects the run rather than publishing a silently cheap one. Five regression tests in
`tests/test_aws_cost_bug_2026_09_05.py` reproduce both failures and fail against the pre-fix code.

Neither existing control would have caught them: the PLAYBOOK fairness filter tests `unpriced_resources`,
which was empty because the tool did not know it had missed anything, and `acspeed/aws_audit.py` reported
run 19 as `ok` while printing `shortfall={'lightsail': (3, 1)}`.

## Instrument version

Every run in this cell measures t1 by the **first-serve** rule, including runs 15 to 19 measured after
the durable-serve fix landed (`475d4b6`, 2026-09-04 17:37). `durable_serving` is called only from the
single-run path (`autorun.py:1763`); `drive_suite`, which runs the medium tiers, never calls it, and no
medium run in any cell carries a `poll_log`. The durable-serve rule is an Easy-tier rule today.
