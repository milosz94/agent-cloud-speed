# AWS (medium tier, disclosed regime): umami + second site + integration + durability (2026-09-05)

| run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | $/mo @ 10k | $/mo @ 500k | $/mo @ 10M | tier |
|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|-----------:|------------:|-----------:|:----:|
| [3](sessions/a0bf606b-edd6-4aa7-a0de-02fe0367d629.jsonl) | 5287 | 867 | 56 | 74 | 1112 | 3178 | $6.60 | $65.62 | $65.62 | $65.62 | 3/5 |
| [5](sessions/a1a0c504-dc73-49bb-81de-5ce9b2399055.jsonl) | 1991 | 551 | 14 | 41 | 1226 | 159 | $1.86 | $66.42 | $66.42 | $66.42 | 5/5 |
| [6](sessions/65712527-02d0-43eb-926f-5f692c43ed71.jsonl) | 2568 | 436 | 13 | 35 | 1780 | 304 | $3.76 | $68.02 | $68.02 | $68.02 | 5/5 |
| [8](sessions/5a38b428-34e3-45cf-adaa-24f108770db7.jsonl) | 2922 | 1300 | 21 | 66 | 1398 | 137 | $7.75 | $81.06 | $81.06 | $81.06 | 5/5 |
| [13](sessions/20ab6eb1-b9dd-46fc-a805-d77a385ed870.jsonl) | 5057 | 671 | 14 | 46 | 874 | 3452 | $7.48 | $65.62 | $65.62 | $65.62 | 3/5 |
| [16](sessions/89ff274c-1762-4c0e-bd51-c755cc6d76d6.jsonl) | 5754 | 596 | 51 | 27 | 4880 | 200 | $5.48 | $66.42 | $66.42 | $66.42 | 5/5 |
| [18](sessions/34c7e294-d3a9-4228-8cbd-52b332c071b3.jsonl) | 5959 | 507 | 10 | 35 | 1450 | 3957 | $8.73 | $100.89 | $100.89 | $100.89 | 3/5 |

n = 7 fair of 19 attempted. Every column is a critical-path second and the per-operation columns sum to
the total (Part 4's M). `agent $` is the deploy round plus the deprovision turn only; it excludes the
agent spend on the four scored operations, which for run 13 is a further $11.58.

## Exclusions, and why

Twelve runs are attempted but not published. They are real data; each would misrepresent the cloud.

| runs | reason |
|---|---|
| 1, 2, 4, 7 | the Lightsail container hostname answered 404 on the first poll, so t1 measured when the hostname appeared, not time-to-serving (edge-4xx contamination, fixed in `04a40ad`) |
| 9, 10 | two Lightsail container services served (umami and site B); the run-rate priced one |
| 11, 12 | one or more Application Load Balancers built and never priced (the tag-independent enumerators were dead; fixed in `64d1fa6`) |
| 14 | a co-provisioned RDS instance built and never priced (same fix) |
| **15** | **site B served from a Lightsail container service; the run-rate took the non-Lightsail branch and priced no Lightsail at all (compute + storage + two load balancers only)** |
| **17** | **two Lightsail container services in two regions (umami in us-east-2, site B in us-east-1); one priced, at $10.00/mo against this cell's $65 to $101 band** |
| **19** | **two Lightsail container services in two regions, plus a Lightsail relational database; one container priced, at $15.00/mo** |

Runs 15, 17 and 19 are a **live, unfixed** defect, not a historical one. `_lightsail_run_rate`
(`acspeed/adapters/aws_runrate.py:645,651`) calls `get-container-services --region {region}` for a single
region derived from the primary app's URL, so a second container service in another region is invisible;
and the non-Lightsail branch never enumerates Lightsail at all, because Lightsail is absent from the
uniform tag inventory. The 2026-09-05 multi-container fix covers several services in **one** region only.
Until that is fixed, any AWS run whose second app lands on Lightsail, or in a second region, will be
silently under-priced.

Neither existing control catches it: the PLAYBOOK fairness filter tests `unpriced_resources`, which is
empty because the tool did not know it had missed anything, and `acspeed/aws_audit.py` reports run 19 as
`ok` while printing `shortfall={'lightsail': (3, 1)}`. Both were verified by hand here from the two
served hostnames.

## Instrument version

Every run in this cell measures t1 by the **first-serve** rule, including runs 15 to 19 measured after
the durable-serve fix landed (`475d4b6`, 2026-09-04 17:37). `durable_serving` is called only from the
single-run path (`autorun.py:1763`); `drive_suite`, which runs the medium tiers, never calls it, and no
medium run in any cell carries a `poll_log`. The durable-serve rule is therefore an Easy-tier rule today.
