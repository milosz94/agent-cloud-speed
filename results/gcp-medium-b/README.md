# gcp (medium tier B, DISCLOSED regime): umami + second site + integration + durability (2026-09-01)

n=1 (run01). This is **Medium B**, the DISCLOSED (plan-upfront) regime: the agent is handed the full
operation plan at deploy time and may schedule with lookahead, versus **Medium A** (`umami-medium`) where
the operations are revealed one at a time (online). Same five operations, verifies, durability goal and
gold; only the information regime differs. The makespan difference between the two regimes is the *value of
plan lookahead* (Part 3, clairvoyance), reported as a tier companion, not the per-run selection excess.

The **Medium** workload is five operations, not just a deploy: **deploy-serve** (umami provisions and
serves), **register** (a probe admin user exists), **deploy-site-b** (a standardized second site, a real
Node app, serves), **integrate** (umami tracking is wired into the second site and a REAL headless-browser
visit records a pageview on a unique sentinel path, 0 -> 1), then a **restart-durability** goal (restart,
and require every durable effect to survive). Architecture: serverless (Cloud Run) umami + managed Postgres
(Cloud SQL) + a second Cloud Run site.

**The run passes all five operations clean (5/5), completed, first-attempt live, content-verified** (real
umami app content, 9673 bytes). The primary was selected by name (`umami-acs7ef2cc38-...`, url not
ambiguous), the second site stood up under its own name (`alcove-acs7ef2cc38-...`), and teardown left
**zero orphans** (verified after the run via the Cloud Run, Cloud SQL and Artifact Registry APIs: 0 each,
for token `acs7ef2cc38`).

## Overview (total wall = all operations; agent $ = the whole workflow)

| run | total wall (s) | agent $ | $/mo (all traffic) | tier |
|----:|---------------:|--------:|-------------------:|:----:|
| 1 | 1438 | $1.74 | $139.12 | 5/5 |

Cost is **usage-metered** (Cloud Run) but reads flat at **$139.12/mo** across 10k to 10M req/mo: this run
holds a Cloud SQL instance plus a min-allocated compute floor that dominates the usage terms. Captured
2026-09-01; egress estimated at 50 KB average response; provisioned floor + active per-second compute
folded in.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 457 | 49 | 90 | 74 | 768 | 1438 |

**Disclosed-regime note (read before comparing to Medium A).** In the plan-upfront regime the agent
front-loads the provisioning into the DEPLOY turn (agent deploy-turn wall **652.7s**), so the register /
site-b / integrate walls above are the harness's post-hoc CONFIRM windows, not the agent doing that work in
separate serialized turns. The durability op is **768s, of which 673s is platform**: restarting the managed
Cloud SQL instance is the long pole. A Medium-A-vs-B *value of plan lookahead* comparison must therefore use
the deploy-turn wall, not a naive sum of these per-op confirm walls.

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` (external concurrent poll, first HTTP status < 500): **456.9s** (n=1, no CI),
= critical_platform **263.5s** + critical_agent **159.0s** (overlap 0). First-attempt liveness **1/1**,
content-verified **1/1**. Capability C is **DEFERRED** (Cloud Run is shell-less, so the off-clock
micro-probes do not apply). Efficiency (Part 3) is **DEFERRED** (no reference-optimal gold authored for this
platform).

Teardown: 0 resources left (the reaper found nothing to reap; a post-run cloud sweep found 0 Cloud Run
services, Cloud SQL instances, or Artifact Registry repos for this run's token).

Redacted, infrastructure-neutral transcripts (one resumed session covering all operations and the teardown)
are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0, n=1).

## Provenance

This run validated two harness fixes that a prior gcp Medium B run (2026-09-01) surfaced, both of which had
corrupted that earlier run and were corrected before this one: (1) the primary URL is now selected by the
app's own name, so the concurrently provisioned second site can never be timed as the app; (2) the umami
wiring check matches umami by service identity (`umami-<token>`), so a snippet pointing at the app via
either of Cloud Run's two URLs for one service is accepted (the unfakeable headless visit still has to
record a real pageview to score). Under these fixes the run is a clean 5/5 with an unambiguous umami primary.
