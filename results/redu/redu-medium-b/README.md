# redu (medium tier B, DISCLOSED regime): umami + second site + integration + durability (2026-09-02)

n=10 (run01-10). **This is a validation-baseline cell, not a paper cloud** - redu is the platform under
development and is excluded from the paper's cross-cloud comparison (like `redu-medium-a`); it is kept here
as a reproducibility reference. **Medium B** is the DISCLOSED (plan-upfront) regime (see the gcp-medium-b
README for the regime and workload). Five operations: deploy-serve, register, deploy-site-b, integrate
(headless-browser visit records a pageview 0 -> 1), and a restart-durability goal.

**All 10 runs pass all five operations clean (5/5), completed, first-attempt live, content-verified,
unambiguous umami primary.** Distinct per-run tokens throughout. Architecture: a umami VM + a managed
Postgres + a second-site VM.

## Overview (total wall = all operations; agent $ = whole workflow incl. deprovision)

| run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | fixed $/mo | $/mo @ 10k | $/mo @ 500k | $/mo @ 10M | tier |
|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|-----------:|-----------:|------------:|-----------:|:----:|
| [1](sessions/487c40b0-96cd-4a15-8d48-cfd5b8dab90b.jsonl) | 456 | 267 | 18 | 44 | 27 | 101 | $6.72 | $44.65 | $44.65 | $44.65 | $44.65 | 5/5 |
| [2](sessions/ddbf8c5d-2201-4fa8-ab7e-1e8e865d46ae.jsonl) | 473 | 331 | 11 | 18 | 22 | 92 | $6.77 | $44.65 | $44.65 | $44.65 | $44.65 | 5/5 |
| [3](sessions/b661c2c6-4d64-4a2f-abb0-aad5353ddba5.jsonl) | 453 | 282 | 10 | 18 | 26 | 116 | $7.22 | $44.65 | $44.65 | $44.65 | $44.65 | 5/5 |
| [4](sessions/24d38e9d-d877-4ebe-bf46-79ee566e70f2.jsonl) | 467 | 333 | 14 | 17 | 17 | 85 | $6.49 | $44.65 | $44.65 | $44.65 | $44.65 | 5/5 |
| [5](sessions/4c66859f-bdf3-4312-aeb3-2d32700980b2.jsonl) | 642 | 308 | 15 | 53 | 35 | 232 | $9.25 | $44.65 | $44.65 | $44.65 | $44.65 | 5/5 |
| [6](sessions/90b1dead-8df2-41b7-8149-0dc1e23e0241.jsonl) | 470 | 342 | 12 | 25 | 17 | 73 | $9.39 | $44.65 | $44.65 | $44.65 | $44.65 | 5/5 |
| [7](sessions/6a7f6276-2ff4-4a53-a23d-fa43b9165915.jsonl) | 471 | 292 | 13 | 48 | 23 | 95 | $6.34 | $44.65 | $44.65 | $44.65 | $44.65 | 5/5 |
| [8](sessions/1c237077-e4ee-4a3f-adf8-b5ff384bcb4d.jsonl) | 434 | 308 | 8 | 14 | 19 | 86 | $7.19 | $44.65 | $44.65 | $44.65 | $44.65 | 5/5 |
| [9](sessions/6e3b0bdf-ec07-4d1d-9d83-5e80663a2dbc.jsonl) | 491 | 328 | 11 | 45 | 19 | 88 | $6.64 | $44.65 | $44.65 | $44.65 | $44.65 | 5/5 |
| [10](sessions/74699e2c-79ad-4ba5-9b9a-20a99b01eb72.jsonl) | 413 | 281 | 12 | 15 | 15 | 90 | $5.48 | $44.65 | $44.65 | $44.65 | $44.65 | 5/5 |

Cost is a **standing** run-rate, flat at **$44.65/mo** (compute VM + managed Postgres). Captured 2026-09-02.

**Column basis (changed 2026-09-06).** The per-operation columns are each operation's critical-path
`split.makespan_s`, matching the aws, gcp and azure cells. This cell previously published `wall_s`,
which runs 5 to 14 seconds longer per operation because it includes the harness verification tail.
Both figures are in every run record; nothing was recomputed or re-run, and `deploy (t1)` is unchanged.

## Provision spine (Part 1/2, deploy-serve only)

Time-to-serving `t1`: **267 to 342s** across the 10 runs (mean ~307s, n=10) - the **fastest and tightest**
spine of any cloud tested (gcp ~426s, aws ~323s, azure ~1631s), with notably low variance. First-attempt
liveness **10/10**, content-verified **10/10**, no ambiguous URL. Capability C is measurable on redu (SSH
shell) but **DEFERRED** here; Efficiency (Part 3) **DEFERRED**.

Teardown: every run `orphan_warning: false` (harness reaper report; a redu-side cloud sweep for these 10
tokens was not separately run).

Redacted transcripts (one resumed session per run) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json`
(residue 0, n=10). Being the platform under test, redu stays out of the paper's anonymized cross-cloud
tables; these numbers are a validation reference only.
