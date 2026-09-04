# azure (medium tier B, DISCLOSED regime): umami + second site + integration + durability (2026-09-04)

n=8 (run01-08; run03 in the raw batch is an older schema the table builder skips, so the published rows
are the 8 schema-conformant runs). **Medium B** is the DISCLOSED (plan-upfront) regime (see the
gcp-medium-b README for the regime and workload description). The five operations are: deploy-serve,
register, deploy-site-b, integrate (headless-browser visit records a pageview 0 -> 1), and a
restart-durability goal.

**All 8 runs pass all five operations clean (5/5), completed, first-attempt live, content-verified,
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

Cost basis follows the hosting: **App Service = standing** ($/mo run-rate, ~$25-40 incl. the Postgres
flexible server); **Container Apps = usage-metered** (~$45-74 across the traffic grid). Captured 2026-09-02
(runs 1-7) and 2026-09-03 (run 8).

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

Same disclosed-regime caveat as gcp: the per-op walls after t1 are confirm windows, not serialized agent
work. Azure is the **slowest** cloud on the spine: App Service / Container Apps provisioning is the long
pole.

## Provision spine (Part 1/2, deploy-serve only)

Time-to-serving `t1` mean **1486.5s** [95% CI 1201.7, 1773.3] (n=8), = critical_platform **1131.3s**
[854.8, 1420.5] + critical_agent **341.6s** [301.1, 389.1], overlap 0. Range 886 to 2757s, materially
slower than gcp (~426s) or redu (~307s). First-attempt liveness **8/8**, content-verified **8/8**, no
ambiguous URL. Read the mean with the 403 caveat above. Capability C **DEFERRED** (App Service /
Container Apps are shell-less for the off-clock probe).

Efficiency (Part 3): floor-ratio **2.3x** [95% CI 1.9, 2.7] (n=6) against F_C = 573.5s (min observed
critical-platform floor over the 6 runs with a usable split); bracket [573.5, 873.8]s. Selection excess 0
(one-operation suite).

Teardown: every run `orphan_warning: false`; 0 azure resource groups carrying a run token on the cloud API
(re-verified account-wide 2026-09-04: 0 resource groups, 0 resources).

Redacted transcripts (one resumed session per run) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json`
(residue 0, n=8).
