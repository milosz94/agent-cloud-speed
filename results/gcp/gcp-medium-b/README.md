# gcp (medium tier B, DISCLOSED regime): umami + second site + integration + durability (2026-09-02)

n=10 (run01-10). **Medium B** is the DISCLOSED (plan-upfront) regime: the agent is handed the full operation
plan at deploy time and may schedule with lookahead, versus **Medium A** (`umami-medium`) where the
operations are revealed one at a time. Same five operations, verifies, durability goal and gold; only the
information regime differs. The makespan gap between the regimes is the *value of plan lookahead* (Part 3,
clairvoyance), a tier companion, not the per-run selection excess.

The **Medium** workload is five operations: **deploy-serve** (umami provisions and serves), **register** (a
probe admin user exists), **deploy-site-b** (a standardized second site serves), **integrate** (umami
tracking is wired into the second site and a REAL headless-browser visit records a pageview on a unique
sentinel path, 0 -> 1), then a **restart-durability** goal (restart, every durable effect must survive).
Architecture: serverless (Cloud Run) umami + managed Postgres (Cloud SQL) + a second Cloud Run site.

**All 10 runs pass all five operations clean (5/5), completed, first-attempt live, content-verified,
unambiguous umami primary, 0 orphans** (teardown cloud-API verified: 0 Cloud Run / Cloud SQL / Artifact
Registry for every token).

## Overview (total wall = all operations; agent $ = whole workflow incl. deprovision)

| run | total wall (s) | agent $ | $/mo (low traffic) | tier |
|----:|---------------:|--------:|-------------------:|:----:|
| 1 | 1438 | $4.57 | $139.12 | 5/5 |
| 2 | 699 | $3.74 | $53.74 | 5/5 |
| 3 | 699 | $6.84 | $139.12 | 5/5 |
| 4 | 703 | $4.43 | $60.31 | 5/5 |
| 5 | 1518 | $5.37 | $42.42 | 5/5 |
| 6 | 889 | $5.61 | $47.18 | 5/5 |
| 7 | 1015 | $6.20 | $42.42 | 5/5 |
| 8 | 692 | $4.08 | $53.74 | 5/5 |
| 9 | 726 | $4.40 | $121.23 | 5/5 |
| 10 | 970 | $6.28 | $29.28 | 5/5 |

Cost is **usage-metered** (Cloud Run) and reads roughly flat per run ($29 to $139/mo across the traffic
grid), dominated by a Cloud SQL instance plus the min-allocated compute floor the agent chose that run.
Captured 2026-09-02; egress estimated at a 50 KB average response.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 457 | 49 | 90 | 74 | 768 | 1438 |
| 2 | 330 | 48 | 83 | 58 | 180 | 699 |
| 3 | 326 | 65 | 97 | 68 | 142 | 699 |
| 4 | 361 | 44 | 56 | 57 | 185 | 703 |
| 5 | 500 | 53 | 91 | 94 | 780 | 1518 |
| 6 | 469 | 51 | 119 | 78 | 172 | 889 |
| 7 | 580 | 50 | 125 | 72 | 189 | 1015 |
| 8 | 346 | 56 | 61 | 61 | 168 | 692 |
| 9 | 375 | 52 | 84 | 69 | 146 | 726 |
| 10 | 518 | 50 | 120 | 81 | 201 | 970 |

**Disclosed-regime note.** In the plan-upfront regime the agent front-loads provisioning into the DEPLOY
turn, so register / site-b / integrate walls are the harness's post-hoc CONFIRM windows, not serialized
agent work. Durability dominates the long runs (restarting the managed Cloud SQL instance is the long pole).
A Medium-A-vs-B *value of plan lookahead* comparison must use the deploy-turn wall, not a naive sum of these
per-op confirm walls.

## Provision spine (Part 1/2, deploy-serve only)

Time-to-serving `t1`: **326 to 580s** across the 10 runs (mean ~426s, n=10, no CI). First-attempt liveness
**10/10**, content-verified **10/10**, no ambiguous URL. Capability C **DEFERRED** (Cloud Run is shell-less).
Efficiency (Part 3) **DEFERRED** (no reference-optimal gold authored).

Teardown: every run `orphan_warning: false`; the full batch verified 0 Cloud Run services, Cloud SQL
instances, or Artifact Registry repos on the cloud API.

Redacted transcripts (one resumed session per run, all operations + teardown) are in `sessions/`; see
`sessions/REDACTION-MANIFEST.json` (residue 0, n=10).

## Run independence and token note

run01, run02, and run06-10 carry distinct per-run tokens. run03-05 share the token **`acs0874f3ab`**: they
came from one `acspeed-run --n 3` batch before the `--n` loop was fixed to mint a fresh token per batch run
(fixed 2026-09-02; run06-10 confirm the fix with 5 distinct tokens). run03-05 are still independent
measurements: they ran strictly sequentially, each tore down before the next, and each t1 is fresh-deploy
scale (not the near-zero a poller would report on a latched leftover). The shared token weakened only the
isolation guarantee for those three, and it was verified to have no measurable speed effect.

## Provenance

These runs validated two harness fixes: (1) the primary URL is selected by the app's own name, so the
concurrently provisioned second site is never timed as the app; (2) the umami wiring check matches umami by
service identity (`umami-<token>`), so a snippet pointing at the app via either of Cloud Run's two URLs is
accepted (the unfakeable headless visit still must record a real pageview to score).
