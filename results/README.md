# Results: umami deploys, easy + medium tier (2026-09-04)

An agent deploys the umami app + its database to a public URL on each cloud; `acspeed` measures each run
externally. App: umami. Model: `claude-opus-5`. How runs land here: `PLAYBOOK.md`.

Each cloud has its own one-table result:

- [redu](redu/) - fixed VM + managed Postgres (n=10)
- [aws](aws/) - Fargate/EC2 + ALB + RDS + public IPv4 (n=10)
- [gcp](gcp/) - Cloud Run (min-instances=1) + Cloud SQL + public IPv4 (n=12)
- [azure](azure/) - App Service or Container Apps (agent's choice) + managed Postgres (n=10)

**Medium tier** goes past "did it deploy": it operates on the live app (register, deploy a second site,
integrate, restart) and re-verifies each effect survived.

Medium A is the ONLINE regime (the agent discovers the next operation as it goes); Medium B is the
DISCLOSED regime (the whole plan is stated upfront).

- [redu-medium-a](redu-medium-a/) - n=10, all 5/5. The only cell with the VM-to-VM network axis populated (n=2 of the 10)
- [aws-medium-a](aws-medium-a/) - n=10
- [gcp-medium-a](gcp-medium-a/) - n=10 (run11 excluded, disclosed: first-poll 403)
- [aws-medium-b](aws-medium-b/) - n=6 fair (runs 1, 2, 4, 7 excluded, disclosed: first-poll 404 from the Lightsail edge)
- [gcp-medium-b](gcp-medium-b/) - n=10
- [azure-medium-b](azure-medium-b/) - n=10 (spine computed over the 9 schema-conformant runs)
- [redu-medium-b](redu-medium-b/) - n=10

Each easy table is `run | t1 (s) | platform (s) | agent (s) | steps | tokens | agent $ | $/mo @ 10k
/ 500k / 10M req`. A standing VM cost stays flat at any traffic; a usage-metered serverless front rises
with it. The cost line-item audit is in `COST-COVERAGE-AUDIT.md`; every transcript is credential- and
infrastructure-redacted (`acspeed sessions`, residue 0).

## Two instrument corrections applied 2026-09-04

**Platform-edge 4xx.** A cloud's edge can answer before the app does (a Lightsail container hostname
404s the moment DNS exists; a Cloud Run IAM denial 403s), stopping the liveness clock early. The harness
now runs a response-origin check on any 4xx (`autorun.is_serving_ex`). Runs measured before this date
are audited by the rule "suspect iff the clock stopped on a 4xx on the first poll"; the affected runs are
excluded and listed in their own cell's README rather than deleted.

**First-poll exclusion (gold 1.0.0 to 1.1.0).** The Part 3 floor and frontier previously excluded *every*
run flagged `served_on_first_poll`. Because a Cloud Run deploy does not return the URL until the revision
is live, that silently excluded **all 33 GCP runs from an entire reported axis, at any n**. Gold 1.1.0
excludes a first-poll run only when a 4xx stopped the clock, or when the served URL does not carry the
run's own token (a possible leftover deployment). Floors and ratios are not comparable across the
version bump.
