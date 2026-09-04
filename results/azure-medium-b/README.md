# azure (medium tier B, DISCLOSED regime): umami + second site + integration + durability (2026-09-04)

n=10 (run01 to run10). run03 is an older schema that `build_tables.py` skips, so the tool-computed
spine below is over the **9 schema-conformant runs**; run03's row is kept in the per-run tables because
its measured values are real and comparable. **Medium B** is the DISCLOSED (plan-upfront) regime (see the
gcp-medium-b README for the regime and workload description). The five operations are: deploy-serve,
register, deploy-site-b, integrate (headless-browser visit records a pageview 0 -> 1), and a
restart-durability goal.

**All 10 runs pass all five operations clean (5/5), completed, first-attempt live, content-verified,
unambiguous umami primary, 0 orphans** (teardown verified: 0 azure resource groups carrying a run token).
Distinct per-run tokens throughout.

> ⚠ **t1 caveat on the three 403 runs (1, 4, 5), added 2026-09-04.** Those runs stopped the liveness clock
> on an HTTP **403** from an `*.azurewebsites.net` host, and App Service serves a 403 page while a web app
> is stopped or still starting. The off-clock oracle later found real umami (200, 9673 bytes) in every
> case, so the runs are genuine 5/5 successes, but their `t1` may be measuring the App Service edge rather
> than umami answering. Unlike the AWS Lightsail case (404 on the FIRST poll, t1 2 to 4x too fast), these
> came after 41 to 112 polls and sit inside the same band as the 200 runs, so any bias is small; it is
> disclosed, not corrected. The response-origin check that decides this automatically landed in the
> harness on 2026-09-04 (after these runs), so a future batch resolves it directly. Runs 2, 6, 7, 8
> stopped on a real 200 and are unaffected.

Architecture varies by run: umami on **App Service** or **Container Apps** (the agent's choice), site B on
App Service, managed **Azure PostgreSQL** for state. Both umami hostings serve HTTPS by default, so azure
never hits the aws HTTP/TLS friction.

## Overview (total wall = all operations; agent $ = whole workflow incl. deprovision)

| run | total wall (s) | agent $ | $/mo (low traffic) | umami hosting | tier |
|----:|---------------:|--------:|-------------------:|:--------------|:----:|
| 1 | 1876 | $5.77 | $37.23 | App Service | 5/5 |
| 2 | 1320 | $3.76 | $45.94 | Container Apps | 5/5 |
| 3 | 3743 | $3.71 | $24.82 | App Service | 5/5 |
| 4 | 1848 | $8.24 | $39.57 | App Service | 5/5 |
| 5 | 1475 | $6.95 | $37.23 | App Service | 5/5 |
| 6 | 2736 | $3.68 | $44.84 | Container Apps | 5/5 |
| 7 | 1865 | $6.44 | $37.23 | App Service | 5/5 |
| 8 | 2120 | $5.68 | $44.84 | Container Apps | 5/5 |
| 9 | 2887 | $7.57 | $37.23 | App Service | 5/5 |
| 10 | 2146 | $4.73 | $37.23 | App Service | 5/5 |

Cost basis follows the hosting: **App Service = standing** ($/mo run-rate, ~$25-40 incl. the Postgres
flexible server); **Container Apps = usage-metered** (~$45-74 across the traffic grid). Captured 2026-09-02
(runs 1-7), 2026-09-03 (run 8) and 2026-09-04 (runs 9, 10).

Seven of the ten runs chose App Service and three chose Container Apps, under free architecture choice.
The two cost bases are therefore **mixed inside this one cell**: a standing $/hr run-rate on the App
Service runs and a per-usage schedule on the Container Apps runs. They are reported on their own bases
and never averaged into a single figure.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 1370 | 44 | 54 | 57 | 351 | 1876 |
| 2 | 1046 | 31 | 57 | 45 | 141 | 1320 |
| 3 | 2757 | 45 | 98 | 62 | 780 | 3743 |
| 4 | 1468 | 54 | 35 | 57 | 234 | 1848 |
| 5 | 886 | 44 | 49 | 58 | 437 | 1475 |
| 6 | 2341 | 29 | 43 | 47 | 276 | 2736 |
| 7 | 1546 | 54 | 30 | 47 | 188 | 1865 |
| 8 | 1807 | 30 | 29 | 46 | 209 | 2120 |
| 9 | 1428 | 52 | 67 | 72 | 233 | 2887 |
| 10 | 1477 | 36 | 111 | 82 | 192 | 2146 |

Same disclosed-regime caveat as gcp: the per-op walls after t1 are confirm windows, not serialized agent
work. Azure is the **slowest** cloud on the spine: App Service / Container Apps provisioning is the long
pole.

## Provision spine (Part 1/2, deploy-serve only)

Time-to-serving `t1` mean **1485.4s** [95% CI 1235.6, 1752.2] (n=9), = critical_platform **1137.5s**
[897.5, 1399.5] + critical_agent **332.0s** [291.9, 379.4], overlap 0. Range 886 to 2341s, materially
slower than gcp (~457s) or redu (~306s). First-attempt liveness **9/9**, content-verified **9/9**, no
ambiguous URL. Read the mean with the 403 caveat above.

Capability C is **N/A, disclosed**: App Service and Container Apps are shell-less, so the off-clock
micro-probes cannot run. This is a legitimate substrate the method cannot probe, not a missing
measurement, and the run is compared on the substrate-neutral time and cost axes instead.

Efficiency (Part 3): floor-ratio **2.6x** [95% CI 2.1, 3.0] (n=9) against F_C = 573.5s (min observed
critical-platform floor); bracket [573.5, 873.8]s = 1.524x wide, floor sensitivity 1.385 to 1.693 at
F_C(1 +/- 0.1). Selection excess 0 by construction (one-operation provision gold). Gold
`provision-deploy/1.1.0`.

**Two runs (6 and 7) served on the very first poll, and both are admitted.** Under gold 1.0.0 any
first-poll run was excluded from the floor and the frontier; 1.1.0 excludes one only when the clock was
stopped by a 4xx (a platform edge answering before the app) or when the served URL does not carry the
run's own token (a possible leftover deployment). Runs 6 and 7 stopped on a real 200 with their own
tokens in the URL, so their `t1` is an honest upper bound and they are valid traces.

Teardown: every run `orphan_warning: false`; 0 azure resource groups carrying a run token on the cloud API
(re-verified account-wide 2026-09-04: 0 resource groups, 0 resources).

Redacted transcripts (one resumed session per run) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json`
(residue 0, n=8).
