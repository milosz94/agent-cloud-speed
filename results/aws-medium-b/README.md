# aws (medium tier B, DISCLOSED regime): umami + second site + integration + durability (2026-09-02)

n=5 (run01-05), **all 5/5 pass**. **Medium B** is the DISCLOSED (plan-upfront) regime (see the gcp-medium-b
README for the regime and workload). Five operations: deploy-serve, register, deploy-site-b, integrate
(headless-browser visit records a pageview 0 -> 1), and a restart-durability goal. Distinct per-run tokens.

aws is the **most variable** cloud, and the route the agent picks dominates everything:

## Overview (total wall = all operations; agent $ = whole workflow incl. deprovision)

| run | total wall (s) | agent $ | $/mo (low) | route | tier |
|----:|---------------:|--------:|-----------:|:------|:----:|
| 1 | 824 | $7.64 | $10.00 | Lightsail + Amplify | 5/5 |
| 2 | 939 | $3.65 | $40.00 | Lightsail + Amplify | 5/5 |
| 3 | 703 | $6.49 | $10.00 | Lightsail + Amplify | 5/5 |
| 4 | 2046 | $4.96 | $66.42 | ALB + RDS | 5/5 |
| 5 | 2624 | $7.76 | $68.02 | ALB + RDS | 5/5 |

Cost is a **standing** run-rate. **HTTPS-native services** (Lightsail container for umami + Amplify for site
B) are cheap ($10-40/mo) and fast; the **ALB + RDS** route is pricier ($66-68/mo) and far slower to
integrate. Captured 2026-09-02.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 160 | 39 | 65 | 48 | 511 | 824 |
| 2 | 216 | 27 | 50 | 48 | 598 | 939 |
| 3 | 250 | 28 | 31 | 42 | 352 | 703 |
| 4 | 551 | 26 | 52 | 1246 | 171 | 2046 |
| 5 | 436 | 25 | 46 | 1803 | 314 | 2624 |

The ALB runs carry **integrate walls of 1246-1803s** vs ~45s for the Lightsail runs: on the ALB route the
agent has to make the cross-origin umami tracking beacon fire (configure CORS, disable umami's bot-check,
often rename the tracker script), which is real work the HTTPS-native route mostly skips.

## Provision spine (Part 1/2, deploy-serve only)

Time-to-serving `t1`: **160 to 551s** across the 5 runs (mean ~323s) - Lightsail fast (160-250s), ALB slower
(436-551s). First-attempt liveness **5/5**, content-verified **5/5**, no ambiguous URL. Capability C and
Efficiency (Part 3) **DEFERRED**.

Teardown: run04 flagged `orphan_warning: true` (a bare target group + stale Lightsail from an older run);
removed post-run and the account re-verified 0 across Lightsail / ELB / RDS / EC2 / ECS / target groups /
Amplify. The reaper does not enumerate Lightsail or Amplify, so those hosts rely on the agent's own teardown.

Redacted transcripts (one resumed session per run) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json`
(residue 0, n=5).
