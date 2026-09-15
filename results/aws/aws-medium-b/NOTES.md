# aws-medium-b: what the numbers mean, and what was left out

The cell table is in `README.md`, in the same shape as every other cell. This file carries the
detail that table cannot: n and its denominator, what the `$/mo` column prices, and why each
unpublished run is unpublished.

**Run numbers in this file are ATTEMPT ids.** `README.md` numbers its rows 1 to 12 in table order.
The mapping is:

| README row | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| attempt id | 3 | 5 | 6 | 8 | 13 | 16 | 22 | 23 | 24 | 25 | 26 | 27 |

n = 12 fair of 27 attempted; 10 of the 12 reached the goal predicate (attempts 5, 6, 8, 16, 22, 23,
24, 25, 26, 27 = README rows 2, 3, 4, 6, 7, 8, 9, 10, 11, 12). Every column is a critical-path second
and the per-operation columns sum to the total (Part 4's M). `agent $` is the deploy round plus the
deprovision turn only; it excludes the agent spend on the four scored operations, which for attempt 13
(README row 5) is a further $11.58.

Runs 22 to 27 are the first six measured after the cost path was rebuilt (2026-09-06). Every
earlier row was priced by the enumerate-then-price adapter; these six were priced from
CloudTrail management events joined to the published price list by usagetype. The six reach the
goal predicate 6/6 against 4/6 for the rows above them, which is a change in the runs and not
in the instrument: the tier score is measured off-clock by the runner and is untouched by the
cost work.

## What the `$/mo` column actually prices, on every row

The run-rate is snapshotted **immediately after the deploy operation and before the suite runs**, by
design: `autorun.py` step 6d prices "the un-perturbed deployment", step 6e then drives the tier. So the
figure is the **deploy bundle's** standing rate, not the whole medium deployment's. Anything the suite
provisions afterwards, site B above all, is never in it. That holds for every row here and for every row
in all six medium cells; it is a property of the measurement, not of these runs. Read the column as
"deploy-leg run-rate" and nothing wider.

## Exclusions, and why

Fifteen runs are attempted but not published. They are real data; each would misrepresent the cloud.

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

**Runs 15 and 18 were withdrawn on 2026-09-06.** Both were published earlier in this cell. Neither
is withdrawn for a defect found in the run: their recorded run-rate comes from the
enumerate-then-price adapter that the 2026-09-06 rebuild replaced, and it has not been
re-verified against what the account was charged. They are held out until it is, rather than
published beside six rows priced by a path that was checked against the bill. Run 15's earlier
exclusion and reinstatement (its RDS post-dates the cost snapshot, exactly as site B does on every
row) is a separate question and remains correct on its own terms.

Both real defects are fixed in `acspeed/adapters/aws_runrate.py`: enumeration now sweeps the candidate
regions and deduplicates by ARN, and only the Lightsail *container service* is dropped from the merge,
with anything else Lightsail priced or else surfaced in `unpriced_resources` so the fairness filter
rejects the run rather than publishing a silently cheap one. Five regression tests in
`tests/test_aws_cost_bug_2026_09_05.py` reproduce both failures and fail against the pre-fix code.

Neither existing control would have caught them: the PLAYBOOK fairness filter tests `unpriced_resources`,
which was empty because the tool did not know it had missed anything, and `acspeed/aws_audit.py` reported
run 19 as `ok` while printing `shortfall={'lightsail': (3, 1)}`.

## Instrument version

This cell is split across the two t1 rules, and the records say which is which. Four of the twelve
published runs (3, 5, 6, 8) measure t1 by the **first-serve** rule and carry no `poll_log`; the other
eight (13, 16, 22 to 27) carry `t1_method: durable-serve/1.0` and a `poll_log`. An earlier version of
this note said every run here was first-serve and that no medium run anywhere carried a `poll_log`;
that was written before the durable-serve rule reached the medium path and the published records
refute it. Read `serving.t1_method` per run rather than this paragraph.
