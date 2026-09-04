# AWS (medium tier, variant B): umami + second site + integration + durability (2026-09-04)

n=6 fair (run03, run05, run06 2026-09-02; run08, run09, run10 2026-09-04). Four further runs are
**excluded and disclosed below**, not deleted. This table replaces the earlier n=5 one, which pooled
three runs now known to be contaminated by a platform-edge defect.

Architecture is **not constant in this cell**, and that is a real result rather than a nuisance: on
run03, run05, run06 and run08 the agent built **EC2 behind an ALB with RDS**, and on run09 and run10 it
built a **Lightsail container service**. The two cost more than 4x apart, so the rows are kept separate
rather than averaged into a single cost figure.

## Result (n=6 fair)

| run | tier | total wall (s) | agent $ | fixed $/mo | deploy t1 (s) | architecture |
|----:|:----:|---------------:|--------:|-----------:|--------------:|:-------------|
| 3 | 3/5 | 5616 | $8.92 | $65.62 | 867 | EC2 + ALB + RDS |
| 5 | 5/5 | 2212 | $4.22 | $66.42 | 551 | EC2 + ALB + RDS |
| 6 | 5/5 | 2747 | $7.21 | $68.02 | 436 | EC2 + ALB + RDS |
| 8 | 5/5 | 3208 | $13.16 | $81.06 | 1300 | EC2 + ALB + RDS |
| 9 | 5/5 | 2930 | $5.23 | $15.00 | 1544 | Lightsail container |
| 10 | 5/5 | 2257 | $6.04 | $15.00 | 1362 | Lightsail container |

`agent $` is the whole workflow (deploy + operations + durability + teardown). Cost is a standing
**run-rate** on both architectures, so it does not move with traffic: the four EC2+ALB+RDS runs sit at
**$65.62 to $81.06/mo** and the two Lightsail runs at **$15.00/mo**, at every traffic level (dated AWS
public list prices, captured per run; egress reported separately).

**run03 scored 3/5.** It is kept, not dropped: Part 5's scoring is checkpoint partial credit, so a run
that provisions and wires but misses a later checkpoint still scores and locates the break point.
Dropping it would bias the cell toward its successes.

## Per operation (wall seconds)

| run | deploy (t1) | register | deploy-site-b | integrate | durability | total |
|----:|------------:|---------:|--------------:|----------:|-----------:|------:|
| 3 | 867 | 67 | 84 | 1223 | 3222 | 5616 |
| 5 | 551 | 26 | 52 | 1246 | 171 | 2212 |
| 6 | 436 | 25 | 46 | 1803 | 314 | 2747 |
| 8 | 1300 | 33 | 77 | 1419 | 149 | 3208 |
| 9 | 1544 | 61 | 45 | 43 | 476 | 2930 |
| 10 | 1362 | 42 | 47 | 43 | 221 | 2257 |

`integrate` splits sharply by architecture: 1223 to 1803s on the four EC2+ALB+RDS runs against 43s on
both Lightsail runs. run03's 3222s durability cycle is where it lost its two checkpoints.

## Provision spine (Part 1/2, the deploy-serve operation only)

Time-to-serving `t1` mean **1009.9s** [95% CI 670.8, 1329.7] (n=6), = critical_platform **559.5s**
[257.4, 915.2] + critical_agent **389.4s** [319.9, 462.6], overlap 0. First-attempt liveness 6/6,
content-verified 6/6.

Efficiency (Part 3): floor-ratio **6.0x** [95% CI 3.9, 8.1] against F_C = 158.3s (min observed
critical-platform floor, n=6); bracket [158.3, 427.3]s = 2.699x wide. Floor sensitivity 2.454 to 2.999
at F_C(1 +/- 0.1). Selection excess 0 by construction (one-operation provision gold). Gold
`provision-deploy/1.1.0`.

## Excluded runs, and why (stated, not hidden)

| run | t1 (s) | why excluded |
|----:|-------:|:-------------|
| 1 | 160.5 | HTTP 404 on the **first poll** from the Lightsail container hostname |
| 2 | 215.5 | same |
| 4 | 250.5 | same |
| 7 | 220.5 | same, **and** the cost tool priced nothing (`cost_run_rate` absent) |

A Lightsail container-service hostname answers 404 the moment DNS exists, before the app serves, so
these four `t1` values measure when the name appeared rather than when the app came up. They average
211.8s against 1009.9s for the six fair runs, so pooling them would have made this cloud look roughly
4x faster than it is. Runs 1, 2 and 4 were published in the earlier n=5 table before the defect was
found on 2026-09-03; they are withdrawn from the headline here and kept on record above.

The harness now refuses this class of stop at measurement time (`autorun.is_serving_ex` runs a
response-origin check on any 4xx against an explicit edge denylist), and `gold.py` 1.1.0 excludes a
first-poll run whose clock was stopped by a 4xx from the Part 3 floor and frontier while admitting one
that stopped on a real 2xx. Run07 is additionally unpriced: it fell in the window when an unpinned
`mcp-proxy-for-aws` dropped the tool the cost path used.

Redacted, infrastructure-neutral transcripts are in `sessions/`; see
`sessions/REDACTION-MANIFEST.json` (residue 0).
