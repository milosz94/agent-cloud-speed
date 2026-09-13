# How acspeed measures

The method in detail. The [README](../README.md) is enough to run the benchmark; read this to
interpret a result, or see the paper for the full treatment.

## What a run does

Every agent turn executes in a fresh Firecracker microVM, so no turn inherits state from the one
before it. The agent receives a task from the suite and a set of live credentials, and drives the
cloud over MCP (the Model Context Protocol, through which a model calls tools a server exposes).
An adapter is an MCP client plus a `CloudProfile`, a mapping from four canonical operations
(`provision`, `wait_ready`, `status`, `teardown`) onto one server's tool names, falling back to that
cloud's CLI where its MCP server cannot create or delete. Readiness is observed from outside the
run: a prober polls the deployment URL until it answers with an HTTP status below 500, and that
timestamp closes the deploy leg. Agent-reported completion is never used. A run is timed in legs:
the deploy, one leg per scored operation, and each durability cycle, in which the services are
restarted and the earlier state must survive.

## What is measured

A span is one timed interval carrying a duration, its dependency on the preceding interval, and an
owner label. **Makespan** is the wall-clock length of a leg, from its first request to the signal
that closes it. The **critical path** is the chain of dependent spans whose length is the makespan.
Each on-path span is labelled with the party the run is waiting on, and on-path time that reaches
neither label is carried explicitly rather than assigned by default:

```
makespan = T_crit_platform + T_crit_agent + T_crit_other
```

`T_crit_platform` is on-path time the cloud holds: a provisioning call that has not returned, a
resource converging to ready, a retry the cloud forced. `T_crit_agent` is on-path time the agent
holds: model inference, orchestration, the calls it issues while polling, its own rework.
`T_crit_other` is held-out idle, on-path time inside the operation's window that neither owner
holds, such as the gap between a provisioning call returning and the agent's next turn beginning.
It is computed as `makespan - T_crit_platform - T_crit_agent`, floored at zero, and reported rather
than dropped; the published tables are refused if a cell's three terms miss its mean makespan by
more than 0.5 s. Attribution is asymmetric by construction: an unassigned gap ending in a platform
event is charged wholly to the platform, so **the agent term is a lower bound and the platform term
an upper bound**. The split separates two causes a makespan alone conflates: an interface that costs
the agent many turns, and a control plane that returns slowly.

Per leg a run records makespan, `critical_platform_s`, `critical_agent_s` and held-out idle. Per run
it records a resource cost (the standing hourly run-rate of what the run leaves provisioned, priced
from a dated snapshot of public list prices, egress separate, and not the cost of completing the
task), session identifiers, and the redacted agent transcript.

## A worked example

`examples/example_trace.json` is a deploy where the agent prepares config *while*
the cloud provisions. The critical path is `s1 -> s2 -> s4 -> s5 -> s6` (23s):

- wall-clock (makespan): **23s**
- critical platform-time: **16s** (provision + boot are the bottleneck)
- critical agent-time: **7s** (plan + deploy + fix)
- raw agent-time: **11s** (its total effort)
- **overlap: 4s** of agent work hidden under the provision = free

This trace has no idle span, so here the split is exactly
`critical_platform + critical_agent = makespan` (16 + 7 = 23). On a real run a third
term appears: `makespan = critical-platform + critical-agent + held-out idle`, the
idle being on-path time inside the operation's window that neither owner holds.

> **The overlap term is 0 on every published run, and this harness cannot report
> otherwise.** `examples/example_trace.json` is a hand-written DAG: `s3` is declared
> to run beside `s2`, which is what produces the 4s. The span builder that reads a
> real agent transcript does not emit a DAG. It emits a single chain, one span per
> consecutive transcript row, each depending on the one before
> (`acspeed/transcript.py:260`), so `raw == critical` for every owner and
> `overlap = raw - critical` is identically 0 no matter what the session did. Over
> the 94 published runs it is 0 on all 359 timed legs. A genuinely concurrent
> session would print the same 0, so that zero is not evidence about the agent.
> The example shows what the *analysis core* computes given concurrency; it is not
> a result the live harness has ever produced.

## Limits

- **The off-path overlap term is 0 and this instrument cannot report otherwise.** Overlap is raw
  agent time minus critical agent time, that is, agent work hidden underneath a platform wait. It is
  0 s on all 359 timed legs (94 deploy, 186 operation, 79 durability), on every cloud and in every
  cell. This is structural, not empirical: the instrument reads a session as one chain of spans, each
  depending on its predecessor, so it cannot represent concurrent work. A genuinely concurrent
  session would also print 0. The zero is a property of the session model, is not evidence about the
  agent, and overlap is not reported as a result.
- **The lane boundary moves with the shape of the call.** A blocking readiness call returns only when
  the resource is ready, so its wait is one platform-owned span; a polling loop breaks the same wait
  into alternating agent-owned and platform-owned spans at nearly identical makespan. The split
  therefore characterizes a cloud's interface and the agent's strategy against it jointly, not the
  cloud alone.
- **The agent factor sits at one level.** One frozen reference agent executed every published run, so
  the corpus varies clouds and their interfaces, not agents.
- **Runs are dated, not version-pinned.** The MCP servers are installed from manifests that pin no
  version (AWS and Azure request `@latest`, the GCP entry names none), and no run record stores the
  model build, the harness version, or the versions that resolved at run time. Each run used whatever
  was current on its own date.
- **Known defects in the published cost numbers** are enumerated in
  [`results/DATA-DEFECTS.md`](../results/DATA-DEFECTS.md), and the per-run records the tables are
  derived from are not part of this release; the tables and the transcripts behind them are.
- **Live runs require Linux with KVM**, since each agent turn executes in a fresh microVM. The
  analysis and reporting code is pure standard-library Python and has no platform requirement.

## Where the interface fits

The interface is not a separate module; it is the coupling lever. A richer tool
removes agent round-trips (fewer critical-agent segments), a server-side blocking
or async call lets the agent overlap instead of poll, and a clean error envelope
cuts self-rework. The framework *measures* an interface's value: run the same
operation on the same cloud with the same reference agent via one interface then
another, and compare the change in critical-agent-time and overlap.

## Adapters are MCP servers

The agent drives the cloud *through MCP*, so an adapter is an **MCP client pointed
at a cloud's MCP server**, not an SDK wrapper. To measure a cloud, give an
`MCPAdapter` a transport (its MCP server) and that cloud's `CloudProfile`, which
maps the canonical operations (`provision`, `wait_ready`, `status`, `teardown`)
to the server's tool names. The analysis is identical across clouds; only the
profile differs. All four profiles carry real tool names against real MCP servers
(`acspeed/adapters/profiles.py`): AWS via `uvx mcp-proxy-for-aws`, GCP via
`npx @google-cloud/cloud-run-mcp`, Azure via `npx @azure/mcp`, and redu via its own
server. Where a cloud's MCP server cannot create or delete something, the profile
says so and the agent falls back to that cloud's CLI over Bash; GCP's teardown
(`gcloud run services delete`) and Azure's create/teardown path are both marked
that way in the file. The published study reports the three hyperscalers.

Because the adapter times the MCP tool calls, it **measures the interface effect**
directly. Same cloud, same 9s boot, only the readiness interface changes:

| interface | makespan | critical-platform | critical-agent |
|---|---|---|---|
| blocking (`wait_for_deployment`) | 9.05 | 9.05 | 0.00 (agent free) |
| poll (status loop) | 9.25 | 0.05 | 9.20 (agent stuck polling) |

A blocking/async tool keeps the wait on the platform and frees the agent to
overlap; a poll loop moves the wait onto the agent. That is the coupling lever,
turned into a measured number (`tests/test_adapters.py`).

## Module to paper section

Each module implements a named piece of the paper. Section numbers are the paper's own.

| Module | Paper section | Implements |
|---|---|---|
| `acspeed/criticalpath.py` | Part 1, Section 2 (the spine) | Build the dependency DAG, longest path = makespan, slack, and the **owner split**: each span is attributed to an owner and the critical time across owners sums to the makespan. The records name two owner lanes plus a held-out idle lane, so `makespan = critical-platform + critical-agent + idle`. Also `overlap = raw - critical` per owner, the off-path work (see the note under the example below). Method reused from the critical-path method (Kelley and Walker 1959; Blumofe and Leiserson 1999) and trace-based critical-path analysis (The Mystery Machine, OSDI 2014; CRISP, USENIX ATC 2022). |
| `acspeed/agenttime.py` | Part 1, Section 3 (the agent segments) | Raw versus critical agent-time: the agent's total effort against only the part of it that sat on the critical path. The difference is the overlap, the agent work that a platform wait hid for free. |
| `acspeed/adapters/` | Part 1, Section 4 (coupling) | MCP-based cloud adapters. To measure a cloud you point an `MCPAdapter` at that cloud's **MCP server**; the same code serves redu, AWS, GCP and Azure via per-cloud `CloudProfile` tool maps. |
| `acspeed/repro.py` | Part 1, Section 5 (reproducibility) | Geometric mean, bootstrap and normal CIs, the CONFIRM repeat-until-tight rule (Maricq et al. 2018), and the non-overlapping-CI comparison rule. |
| `acspeed/operation.py` | Part 2, Sections 2-4, 6 | The operation as a **composite over atoms**: the seven-slot schema, the three-type typology (provision / operate-mutate / deprovision) via a measured `profile()`, the three atom registers with the platform/agent invariant, and the **milestone split** that reuses SSH-ready to separate the cited lower half from the novel upper-half agent/platform decomposition. Executable falsifiability checks (`is_schema_conformant`). |
| `acspeed/session.py` | Part 2, Section 7 | Session as a **trace of operations**, and `efficiency(actual, optimal)` = excess critical-path wall-clock vs an optimal reference trace, with the exact identity `excess = selection_excess + execution_excess`. Constructing the reference is Part 3 (`reference.py`). |
| `acspeed/reference.py` | Part 3, Sections 1-4 | The **operation-state graph** and its floor cost-to-go `V_F`; the reference-optimal floor makespan `F_C = V_F(start)` as a shortest cost-to-go; the exact decomposition of **excess over the floor** into per-decision advantages (telescoping to `M_actual - F_C`) and the floor-twin **selection vs execution** split; the two-ratio bracket `F_C <= optimum <= best-achieved` (structural floor + DEA frontier) and the refutable gold. The decomposition is against the **floor**, not a given optimum, per the Part 3 rework. |
| `acspeed/weighting.py` | Part 4, Sections 1-4 | Aggregation with **no chosen weight**: `suite_total` (the total-time headline `E_X`), `geomean_ratio` (the reference-invariant companion `G_X`; Fleming and Wallace 1986), `suite_verdict` (report the ranking only when both agree), `leave_one_out` (the drop-any-task sensitivity), and the matched-block per-task `more_efficient` (non-overlapping CIs). The `(wall-clock, resource-cost)` **cost-performance frontier** under the Pareto-Koopmans criterion (`dominates`, `pareto_frontier`, `classify_run`, `is_tradeoff`): a run that spends more cost for no less time carries a positive cost slack and is dominated, so speed cannot be bought (Pareto 1896; DEA, Charnes-Cooper-Rhodes 1978; Banker-Charnes-Cooper 1984). |

### In the harness, not in the paper

These four exist as working, tested code, but they produced **no measurement in the published
94-run wave**, so the paper does not describe them and no published number depends on them. They
are kept here for anyone who wants to exercise them, and flagged so nobody mistakes them for part
of the published method.

| Module | What it does |
|---|---|
| `acspeed/capability.py` | Reference-ratio normalization `r = measured/reference` and the delivered-capability index `DCI` (weighted geometric mean; Fleming and Wallace 1986); dominant axis by measurement (USE method; Roofline). Measures what machine the cloud actually delivered, to normalize a fast cloud against a fast machine. |
| `acspeed/probes.py` | Parsers for the delivered-capability probes: `sysbench` (compute), STREAM (memory), `fio` (disk), and the network axis: **VM-to-VM** `iperf3` throughput + `ping` RTT between two of the operation's own instances over the tenant private network (C17), a scored axis only for multi-VM operations (single-VM: N/A + nominal NIC disclosed, C18). Runner wrappers that shell out live in `runners.py`. |
| `acspeed/discriminator.py` | The fixed-plus-variable fit `T(rate) = t_fixed + W/rate`: intercept = control-plane floor, slope = data-plane work (Hockney; LogP; Amdahl; Mao and Humphrey 2012). Plus the Karp-Flatt serial-fraction falsifier (CACM 1990). |
| `acspeed/agenttime.py` (component split) | Breaking agent-time into inference / orchestration / wait / rework. Only `inference` was ever reachable from a transcript, so the four-way split is unexercised. Inference seconds `= TTFT + TPOT * (output_tokens - 1)` (MLPerf; TTFT already prices the first token). |

## Scope and status

**The measurement path is complete and has been run for real.** The analysis core
is fully tested, the live harness drives real agents against real cloud accounts,
and the 94 runs in [`results/`](../results/) were produced by the code in this repo.

[Limits](#limits) covers what the *measurement* does and does not establish. This section is
about the *implementation*, so nothing here reads as more finished than it is:

- **Live runs need Claude Code.** The Codex path is implemented and tested for
  reading transcripts, but the microVM does not yet carry Codex. See
  [Requirements](../README.md#requirements).
- **The capability probes are unexercised.** `runners.py` shells out to `sysbench`,
  STREAM, `fio` and `iperf3` on the target VM. Nothing in the published wave ran
  them, so that path has no measurement behind it. See
  [In the harness, not in the paper](#module-to-paper-section).

## Sessions

`sessions/` holds the raw agent deploy/measurement transcripts behind the numbers, redacted so the
paper's self-reported claims (e.g. "none of these observations appeared in the development sessions")
are checkable rather than taken on trust. They show what the agent did, the app it deployed and the
timing, not any secret or how the cloud is built underneath. Regenerate the bundle with:

```
acspeed sessions --in <transcripts-dir> --out sessions      # add --keep-substrate for internal use
```

Removed: generated app secrets (passwords, API tokens, encryption keys), OAuth bearer/JWT,
private-key and SSH-key material, connection-string passwords; and, by default, the infrastructure
setup -- provider technology names, control-plane hostnames and internal IPs. Kept: the agent's tool
calls, the deployed app URLs, the platform brand. Redaction touches only secret- or substrate-bearing
string values, so timestamps, token counts and message structure are unchanged and each session still
reconstructs the exact trace and token totals the core computes (`acspeed/transcript.py`); the bundler
re-scans every output and fails loud on any residue. `sessions/REDACTION-MANIFEST.json` records what
was removed per file. See `acspeed/redact.py`.
