# aws (medium tier B, DISCLOSED regime): umami + second site + integration + durability (2026-09-02)

n=6 (run01-06), **5/6 pass**. **Medium B** is the DISCLOSED (plan-upfront) regime (see the gcp-medium-b
README for the regime and workload). Five operations: deploy-serve, register, deploy-site-b, integrate
(headless-browser visit records a pageview 0 -> 1), and a restart-durability goal. Distinct per-run tokens.

aws is the **most variable** cloud, and the route the agent picks dominates everything:

## Overview (total wall = all operations; agent $ = whole workflow incl. deprovision)

| run | total wall (s) | agent $ | $/mo (low) | route | tier |
|----:|---------------:|--------:|-----------:|:------|:----:|
| 1 | 824 | $7.64 | $10.00 | Lightsail + Amplify | 5/5 |
| 2 | 939 | $3.65 | $40.00 | Lightsail + Amplify | 5/5 |
| 3 | 5464 | $18.77 | $65.62 | ALB + RDS | **3/5** |
| 4 | 703 | $6.49 | $10.00 | Lightsail + Amplify | 5/5 |
| 5 | 2046 | $4.96 | $66.42 | ALB + RDS | 5/5 |
| 6 | 2624 | $7.76 | $68.02 | ALB + RDS | 5/5 |

Cost is a **standing** run-rate. **HTTPS-native services** (Lightsail container for umami + Amplify for site
B) are cheap ($10-40/mo) and fast; the **ALB + RDS** route is pricier ($65-68/mo) and far slower to
integrate. Captured 2026-09-02.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 1 | 160 | 39 | 65 | 48 | 511 | 824 |
| 2 | 216 | 27 | 50 | 48 | 598 | 939 |
| 3 | 867 | 67 | 84 | 1223 | 3222 | 5464 |
| 4 | 250 | 28 | 31 | 42 | 352 | 703 |
| 5 | 551 | 26 | 52 | 1246 | 171 | 2046 |
| 6 | 436 | 25 | 46 | 1803 | 314 | 2624 |

The ALB runs carry **integrate walls of 1223-1803s** vs ~45s for the Lightsail runs: on the ALB route the
agent has to make the cross-origin umami tracking beacon fire (configure CORS, disable umami's bot-check,
often rename the tracker script), which is real work the HTTPS-native route mostly skips.

## The failure (run03) is a FAIR agent failure, not an instrument artifact

**run03 (ALB) scored 3/5** and is **included** (not excluded): it is a legitimate failure of the task, kept
in the cell honestly. Root cause, traced: the agent **could not get the cross-origin tracking beacon to
work** - 28 CORS errors, and it never renamed the umami tracker script - so the harness's real headless
visit fired but the beacon was blocked (pageview `0 -> 0`); integrate then failed and durability ground on
(total wall 5464s / 91 min). run06, the **same ALB route**, configured CORS + renamed the tracker
(`TRACKER_SCRIPT_NAME`) and the beacon landed (`api/send` 200, pageview `0 -> 1`). The harness behaved
identically both times (a real-UA headless visit); the difference is the **agent's own umami configuration**.
Note: run03 actually disabled umami's bot-check more aggressively than the passing runs, so bot-check was not
the cause - the cross-origin beacon config was. This is agent-skill variance on the harder ALB path.

## Provision spine (Part 1/2, deploy-serve only)

Time-to-serving `t1`: **160 to 867s** across the 6 runs (mean ~413s) - Lightsail fast (160-250s), ALB slower
(436-867s). First-attempt liveness **6/6**, content-verified **6/6**, no ambiguous URL. Capability C and
Efficiency (Part 3) **DEFERRED**.

Teardown: run05 flagged `orphan_warning: true` (a bare target group + stale Lightsail from an older run);
removed post-run and the account re-verified 0 across Lightsail / ELB / RDS / EC2 / ECS / target groups /
Amplify. The reaper does not enumerate Lightsail or Amplify, so those hosts rely on the agent's own teardown.

Redacted transcripts (one resumed session per run) are in `sessions/`; see `sessions/REDACTION-MANIFEST.json`
(residue 0, n=6).
