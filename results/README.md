# Results: umami deploys, easy + medium tier (2026-09-04)

An agent deploys the umami app + its database to a public URL on each cloud; `acspeed` measures each run
externally. App: umami. Model: `claude-opus-5`. How runs land here: `PLAYBOOK.md`.

## Layout

One folder per cloud, one subfolder per **cell**. A cell is one cloud x tier x regime cut, and it is the
unit that carries its own README, its own `sessions/` bundle, and its own n. Processing a run into a cell:
`PLAYBOOK.md`. Known defects in the published numbers: `DATA-DEFECTS.md`.

**Easy tier** is the deploy alone: the app and its database provision and serve a public URL.

**Medium tier** goes past "did it deploy": it operates on the live app (register, deploy a second site,
integrate, restart) and re-verifies each effect survived. Medium A is the ONLINE regime (the agent
discovers the next operation as it goes); Medium B is the DISCLOSED regime (the whole plan is stated
upfront).

In the medium tables every time is a **critical-path** second (Part 1's spine: platform-time and
agent-time do not add, they interleave; off-path work is free in wall-clock and shows up on the cost axis,
never the time axis). Each per-operation column is that operation's critical path, and **total = M**, the
per-task critical-path wall-clock summed over the operations, so the row's operation columns sum to its
total. This is one definition for both regimes: online has no cross-operation overlap so M equals the
serial sum, disclosed does not, which is what makes the A-vs-B comparison honest.

### [aws/](aws/)

- [aws-easy](aws/aws-easy/) - n=10
- [aws-medium-a](aws/aws-medium-a/) - n=10
- [aws-medium-b](aws/aws-medium-b/) - n=6 fair (runs 1, 2, 4, 7 excluded, disclosed: first-poll 404 from the Lightsail edge)

### [gcp/](gcp/)

- [gcp-easy](gcp/gcp-easy/) - n=12
- [gcp-medium-a](gcp/gcp-medium-a/) - n=10
- [gcp-medium-b](gcp/gcp-medium-b/) - n=10

### [azure/](azure/)

- [azure-easy](azure/azure-easy/) - n=10
- [azure-medium-a](azure/azure-medium-a/) - n=10
- [azure-medium-b](azure/azure-medium-b/) - n=10

### [redu/](redu/)

- [redu-easy](redu/redu-easy/) - n=10
- [redu-medium-a](redu/redu-medium-a/) - n=10
- [redu-medium-b](redu/redu-medium-b/) - n=10

Each easy table is `run | t1 (s) | platform (s) | agent (s) | steps | tokens | agent $ | fixed $/mo |
$/mo @ 10k / 500k / 10M req` (same columns on every cloud; a usage-metered front with no standing
rate shows `-` for `fixed $/mo`). A standing VM cost stays flat at any traffic; a usage-metered serverless front rises
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
