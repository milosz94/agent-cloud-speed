# Results: umami deploys, easy + medium tier (2026-09-04)

An agent deploys the umami app + its database to a public URL on each cloud; `acspeed` measures each run
externally. App: umami. Model: `claude-opus-5`. How runs land here: `PLAYBOOK.md`.

## Layout

One folder per cloud, one subfolder per **cell**. A cell is one cloud x tier x regime cut, and it is the
unit that carries its own README, its own `sessions/` bundle, and its own n. Processing a run into a cell:
`PLAYBOOK.md`.

**Easy tier** is the deploy alone: the app and its database provision and serve a public URL.

**Medium tier** goes past "did it deploy": it operates on the live app (register, deploy a second site,
integrate, restart) and re-verifies each effect survived. Medium A is the ONLINE regime (the agent
discovers the next operation as it goes); Medium B is the DISCLOSED regime (the whole plan is stated
upfront).

In the medium tables every time is a **critical-path** second (Part 1's spine: platform-time and
agent-time do not add, they interleave; off-path work is free in wall-clock and shows up on the cost axis,
never the time axis). Each per-operation column is that operation's critical path, with one
exception: **deploy (t1)** is the externally polled time-to-serving, which ends after the deploy leg
itself does. The row's operation columns sum to its `total`, and because of that exception `total` is
**not** Part 4's `M`: M takes the deploy leg's own critical path instead. On aws-medium-a run 1 that is
666 s deploy and 3060 s total here against 652 s and 3046 s in the paper. Same run, two stated
definitions; the top-level `README.md` carries the same note. Both regimes use one definition: each leg's own critical
path, summed unconditionally. The published operation windows are disjoint in both (zero overlapping
windows across all 81 medium rows), so neither regime is credited for overlap and the A-vs-B comparison
stays on one basis.

### [aws/](aws/)

- [aws-easy](aws/aws-easy/) - n=10
- [aws-medium-a](aws/aws-medium-a/) - n=10
- [aws-medium-b](aws/aws-medium-b/) - n=12

This benchmark names the clouds it measures. `../BENCHMARK-TERMS.md` records what each provider's own
terms say about publishing benchmark results, quoted from the primary source and dated, and how this
project meets them. All three permit publication; the condition they share is that the disclosure
carry enough to replicate it, which is what this tree is.

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
with it. Every transcript is credential- and
infrastructure-redacted (`acspeed sessions`, residue 0).

