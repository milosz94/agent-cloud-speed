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

| run | total wall (s) | agent $ | $/mo | tier |
|----:|---------------:|--------:|-----:|:----:|
| 1 | 487 | $6.72 | $44.65 | 5/5 |
| 2 | 504 | $6.77 | $44.65 | 5/5 |
| 3 | 484 | $7.22 | $44.65 | 5/5 |
| 4 | 497 | $6.49 | $44.65 | 5/5 |
| 5 | 671 | $9.25 | $44.65 | 5/5 |
| 6 | 506 | $9.39 | $44.65 | 5/5 |
| 7 | 509 | $6.34 | $44.65 | 5/5 |
| 8 | 465 | $7.19 | $44.65 | 5/5 |
| 9 | 521 | $6.64 | $44.65 | 5/5 |
| 10 | 444 | $5.48 | $44.65 | 5/5 |

Cost is a **standing** run-rate, flat at **$44.65/mo** (compute VM + managed Postgres). Captured 2026-09-02.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 267 | 24 | 49 | 39 | 107 | 487 |
| 2 | 331 | 17 | 23 | 35 | 98 | 504 |
| 3 | 282 | 16 | 24 | 39 | 122 | 484 |
| 4 | 333 | 21 | 23 | 30 | 91 | 497 |
| 5 | 308 | 21 | 58 | 47 | 237 | 671 |
| 6 | 342 | 19 | 30 | 30 | 84 | 506 |
| 7 | 292 | 21 | 55 | 40 | 101 | 509 |
| 8 | 308 | 15 | 19 | 32 | 92 | 465 |
| 9 | 328 | 18 | 50 | 32 | 94 | 521 |
| 10 | 281 | 19 | 21 | 28 | 96 | 444 |

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
