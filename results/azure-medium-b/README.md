# azure (medium tier B, DISCLOSED regime): umami + second site + integration + durability (2026-09-02)

n=7 (run01-07). **Medium B** is the DISCLOSED (plan-upfront) regime (see the gcp-medium-b README for the
regime and workload description). The five operations are: deploy-serve, register, deploy-site-b, integrate
(headless-browser visit records a pageview 0 -> 1), and a restart-durability goal.

**All 7 runs pass all five operations clean (5/5), completed, first-attempt live, content-verified,
unambiguous umami primary, 0 orphans** (teardown verified: 0 azure resource groups carrying a run token).
Distinct per-run tokens throughout.

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

Cost basis follows the hosting: **App Service = standing** ($/mo run-rate, ~$25-40 incl. the Postgres
flexible server); **Container Apps = usage-metered** (~$45-74 across the traffic grid). Captured 2026-09-02.

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

Same disclosed-regime caveat as gcp: the per-op walls after t1 are confirm windows, not serialized agent
work. Azure is the **slowest** cloud on the spine: App Service / Container Apps provisioning is the long
pole.

## Provision spine (Part 1/2, deploy-serve only)

Time-to-serving `t1`: **886 to 2757s** across the 7 runs (mean ~1631s, n=7, no CI) - materially slower than
gcp (~426s) or redu (~307s). First-attempt liveness **7/7**, content-verified **7/7**, no ambiguous URL.
Capability C **DEFERRED** (App Service / Container Apps are shell-less for the off-clock probe). Efficiency
(Part 3) **DEFERRED**.

Teardown: every run `orphan_warning: false`; 0 azure resource groups carrying a run token on the cloud API.

Redacted transcripts (one resumed session per run) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json`
(residue 0, n=7).
