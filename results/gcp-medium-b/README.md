# gcp (medium tier B, DISCLOSED regime): umami + second site + integration + durability (2026-09-02)

n=5 (run01-05). This is **Medium B**, the DISCLOSED (plan-upfront) regime: the agent is handed the full
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

**All five runs pass all five operations clean (5/5), completed, first-attempt live, content-verified**
(real umami app content). Every run's primary was selected by name (`umami-<token>-...`, url not ambiguous)
and the second site stood up under its own name (`alcove-<token>-...`).

## Overview (total wall = all operations; agent $ = the whole workflow, incl. deprovision)

| run | total wall (s) | agent $ | $/mo @ 10k req | tier |
|----:|---------------:|--------:|---------------:|:----:|
| 1 | 1438 | $4.57 | $139.12 | 5/5 |
| 2 | 699 | $3.74 | $53.74 | 5/5 |
| 3 | 699 | $6.84 | $139.12 | 5/5 |
| 4 | 703 | $4.43 | $60.31 | 5/5 |
| 5 | 1518 | $5.37 | $42.42 | 5/5 |

Cost is **usage-metered** (Cloud Run) and reads roughly flat per run across 10k to 10M req/mo, because a
Cloud SQL instance plus a min-allocated compute floor dominates the usage terms; the per-run figure varies
($42 to $139/mo) with the min-instance / SQL configuration the agent chose that run. Captured 2026-09-02;
egress estimated at a 50 KB average response; provisioned floor + active per-second compute folded in.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 457 | 49 | 90 | 74 | 768 | 1438 |
| 2 | 330 | 48 | 83 | 58 | 180 | 699 |
| 3 | 326 | 65 | 97 | 68 | 142 | 699 |
| 4 | 361 | 44 | 56 | 57 | 185 | 703 |
| 5 | 500 | 53 | 91 | 94 | 780 | 1518 |

**Disclosed-regime note (read before comparing to Medium A).** In the plan-upfront regime the agent
front-loads the provisioning into the DEPLOY turn (for run01, the agent deploy-turn wall was **652.7s**), so
the register / site-b / integrate walls above are the harness's post-hoc CONFIRM windows, not the agent
doing that work in separate serialized turns. The durability op dominates the two long runs (768s and 780s,
mostly platform: restarting the managed Cloud SQL instance is the long pole). A Medium-A-vs-B *value of plan
lookahead* comparison must therefore use the deploy-turn wall, not a naive sum of these per-op confirm walls.

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` (external concurrent poll, first HTTP status < 500): **326 to 500s** across the five
runs (mean ~395s, n=5, no CI), = critical_platform + critical_agent (overlap 0). First-attempt liveness
**5/5**, content-verified **5/5**, no ambiguous URL on any run. Capability C is **DEFERRED** (Cloud Run is
shell-less, so the off-clock micro-probes do not apply). Efficiency (Part 3) is **DEFERRED** (no
reference-optimal gold authored for this platform).

Teardown: every run reported `orphan_warning: false`. run01 + run02 were additionally **cloud-API verified**
after the run (0 Cloud Run services, Cloud SQL instances, or Artifact Registry repos for their tokens);
run03-05 rest on the harness reaper's own check.

Redacted, infrastructure-neutral transcripts (one resumed session per run, covering all operations and the
teardown) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json` (residue 0, n=5).

## Run independence and token note

run01 (`acs7ef2cc38`) and run02 (`acs24148869`) carry distinct per-run tokens. run03-05 share the token
**`acs0874f3ab`**: they came from one `acspeed-run --n 3` batch, and at the time the `--n` loop minted a
single run token and reused it across the batch's runs (a harness bug, since fixed so each batch run mints
its own token). These three are still independent measurements: they ran strictly sequentially (finished
23:11, 23:33, 00:08), each tore its resources down before the next started, and each t1 is fresh-deploy
scale (326 / 361 / 500s) rather than the near-zero a poller would report if a later run had latched a
leftover service. So the isolation guarantee was weaker for run03-05, but the deploys were genuinely fresh.

## Provenance

These runs validated two harness fixes that a prior gcp Medium B run (2026-09-01) surfaced: (1) the primary
URL is selected by the app's own name, so the concurrently provisioned second site can never be timed as the
app; (2) the umami wiring check matches umami by service identity (`umami-<token>`), so a snippet pointing at
the app via either of Cloud Run's two URLs for one service is accepted (the unfakeable headless visit still
has to record a real pageview to score). Under these fixes every run is a clean 5/5 with an unambiguous
umami primary.
