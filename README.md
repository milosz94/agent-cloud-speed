# acspeed

A reference implementation of **Parts 1 and 2** of a cloud-agnostic framework for
measuring **agent-cloud operation efficiency** as a *speed* test.

It implements the computational core of the paper's Part 1 baseline: the
critical-path split of wall-clock into agent versus platform time, the measured
delivered-capability normalization, the control-plane versus data-plane
discriminator, the agent-time decomposition, and the reproducibility statistics.
**Part 2** adds the operation model: the seven-slot schema, the three-type
typology, the atom registers, the reused SSH-ready milestone split, and the
session-as-trace efficiency metric.
Everything here is pure Python (standard library only), so you can clone and run
it without installing anything.

The measurement *method* is the contribution; specific clouds are validation
instances (Part 5), not the subject.

## Install / run

```bash
# no dependencies; Python >= 3.10
python -m unittest discover -s tests -t . -v      # run the test suite
python examples/run_example.py                    # worked example
python -m acspeed agent-time examples/example_trace.json
python -m acspeed critical-path examples/example_trace.json
```

## What maps to what (paper Parts 1-2)

| Module | Paper section | Implements |
|---|---|---|
| `acspeed/criticalpath.py` | Section 2 (the spine) | Build the dependency DAG, longest path = wall-clock, slack, and the **owner split**: `wall-clock = critical-platform + critical-agent`, `overlap = raw - critical`. Method reused from the critical-path method (Kelley and Walker 1959; Blumofe and Leiserson 1999) and trace-based critical-path analysis (The Mystery Machine, OSDI 2014; CRISP, USENIX ATC 2022). |
| `acspeed/capability.py` | Section 3 (platform) | Reference-ratio normalization `r = measured/reference` and the delivered-capability index `DCI` (weighted geometric mean; Fleming and Wallace 1986); dominant axis by measurement (USE method; Roofline). |
| `acspeed/discriminator.py` | Section 3 (platform) | The fixed-plus-variable fit `T(rate) = t_fixed + W/rate`: intercept = control-plane floor, slope = data-plane work (Hockney; LogP; Amdahl; Mao and Humphrey 2012). Plus the Karp-Flatt serial-fraction falsifier (CACM 1990). |
| `acspeed/agenttime.py` | Section 4 (agent) | Raw vs critical agent-time and the component split (inference / orchestration / wait / rework). Inference seconds `= TTFT + TPOT * output_tokens` (MLPerf). |
| `acspeed/repro.py` | Section 6 | Geometric mean, bootstrap and normal CIs, the CONFIRM repeat-until-tight rule (Maricq et al. 2018), and the non-overlapping-CI comparison rule. |
| `acspeed/probes.py` | Section 3 (platform) | Parsers for the delivered-capability probes: `sysbench` (compute), STREAM (memory), `fio` (disk), `iperf3` (network). Runner wrappers that shell out live in `runners.py`. |
| `acspeed/operation.py` | Part 2, Sections 2-4, 6 | The operation as a **composite over atoms**: the seven-slot schema, the three-type typology (provision / operate-mutate / deprovision) via a measured `profile()`, the three atom registers with the platform/agent invariant, and the **milestone split** that reuses SSH-ready to separate the cited lower half from the novel upper-half agent/platform decomposition. Executable falsifiability checks (`is_schema_conformant`). |
| `acspeed/session.py` | Part 2, Section 7 | Session as a **trace of operations**, and `efficiency(actual, optimal)` = excess critical-path wall-clock vs an optimal reference trace, with the exact identity `excess = selection_excess + execution_excess`. Constructing the optimal trace is Part 3. |
| `acspeed/adapters/` | Section 5 / coupling | MCP-based cloud adapters. To measure a cloud you point an `MCPAdapter` at that cloud's **MCP server**; the same code serves redu, AWS, GCP and Azure via per-cloud `CloudProfile` tool maps. |

## The idea in one example

`examples/example_trace.json` is a deploy where the agent prepares config *while*
the cloud provisions. The critical path is `s1 -> s2 -> s4 -> s5 -> s6` (23s):

- wall-clock (makespan): **23s**
- critical platform-time: **16s** (provision + boot are the bottleneck)
- critical agent-time: **7s** (plan + deploy + fix)
- raw agent-time: **11s** (its total effort)
- **overlap: 4s** of agent work hidden under the provision = free

So `critical_agent + critical_platform = wall-clock`, and the agent's efficiency
shows up as the 4s it overlapped rather than added to the clock.

## Where the interface (MCP, raw API, IaC) fits

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
profile differs. redu's profile uses the real redu MCP tool names
(`create_instance`, `wait_for_deployment`, ...); `aws` / `gcp` / `azure` are
stubs to fill in against their MCP servers during Part 5. Publishing reports the
three hyperscalers; redu uses the same tool separately.

Because the adapter times the MCP tool calls, it **measures the interface effect**
directly. Same cloud, same 9s boot, only the readiness interface changes:

| interface | makespan | critical-platform | critical-agent |
|---|---|---|---|
| blocking (`wait_for_deployment`) | 9.05 | 9.05 | 0.00 (agent free) |
| poll (status loop) | 9.25 | 0.05 | 9.20 (agent stuck polling) |

A blocking/async tool keeps the wait on the platform and frees the agent to
overlap; a poll loop moves the wait onto the agent. That is the coupling lever,
turned into a measured number (`tests/test_adapters.py`).

## Scope and status

This is the offline computational core (fully tested). Running the probes against
real cloud instances (the `runners.py` wrappers) needs the tools installed on the
target VM and is exercised in Part 5's validation, not here. Reference-constant
values and exact probe configurations are disclosed choices to be fixed before
publishing numbers.

## License

MIT (see `LICENSE`). Fill in the copyright holder before publishing.
