# acspeed

A reference implementation of **Parts 1, 2, 3, and 4** of a cloud-agnostic framework for
measuring **agent-cloud operation efficiency** as a *speed* test.

It implements the computational core of the paper's Part 1 baseline: the
critical-path split of wall-clock into agent versus platform time, and the
reproducibility statistics. The harness also carries machinery the paper does
not use and does not describe: the delivered-capability probe and its
normalization, the control-plane versus data-plane discriminator, and the
four-component agent-time decomposition. Those produced no measurement in the
published wave, which is why the paper drops them; they remain here as working
code for anyone who wants to exercise them.
**Part 2** adds the operation model: the seven-slot schema, the three-type
typology, the atom registers, the reused SSH-ready milestone split, and the
session-as-trace efficiency metric. **Part 3** adds the reference-optimal: the
operation-state graph and its floor cost-to-go, the exact decomposition of the
excess *over the floor* (telescoping per-decision advantages and the floor-twin
selection/execution split), and the two-ratio bracket on the true optimum.
**Part 4** adds the weighting: aggregation by summation of critical-path seconds
(no chosen weight), the total-time headline with its geometric-mean companion and
drop-any-task sensitivity, and the two-input `(wall-clock, resource-cost)`
Pareto-Koopmans cost-performance frontier that keeps capability selection honest so
speed cannot be bought. Everything here is pure Python (standard library only), so
you can clone and run it without installing anything.

The measurement *method* is the contribution; specific clouds are validation
instances (Part 5), not the subject.

## What runs where

acspeed has two halves with very different requirements. Read this table before installing.

| | what it does | Linux | macOS | Windows |
|---|---|---|---|---|
| **Analysis** (`acspeed`, tests, examples) | reads traces and run records, computes the splits and tables | yes | yes | yes |
| **Live benchmark** (`acspeed-run`) | drives a real agent against a real cloud and times it | yes | **no** | **no** |

The live benchmark runs every agent turn inside a fresh **Firecracker microVM**, which needs
`/dev/kvm`. Firecracker is Linux-only, so `acspeed-run` is Linux-only. It does **not** fall back to
running on the host: without the microVM it refuses to start, and `--no-sandbox` triggers the same
refusal, because a host run produces no platform/agent split, no session id and no cost, and would
spend real money to produce a record that cannot become a published row.

On macOS or Windows, run the live benchmark inside a Linux VM or WSL2 that exposes `/dev/kvm`
(nested virtualization). The analysis half needs none of that and runs natively everywhere.

## Requirements

**Analysis half** - Python >= 3.10 and nothing else. There are no runtime dependencies; the package
is standard library only.

**Live benchmark**, in addition:

- **Linux with KVM.** `ls /dev/kvm` must succeed. On a physical machine, enable virtualization in
  the BIOS; in a cloud VM, enable nested virtualization.
- **The agent CLI.** `acspeed-run` shells out to `claude -p ...`, so Claude Code must be installed
  and authenticated (`setup.sh` installs it; authentication is yours to do). Any model string you
  pass with `--model` must be one your account can use. **Only Claude Code is supported**: the
  runner hardcodes the `claude` invocation and parses its JSON result and session id, so Codex and
  other agent CLIs need a code change in `autorun.py`, not a config switch.
- **Node.js** (for `npx`) and **uv** (for `uvx`), which is how the per-cloud MCP servers are
  launched: `mcp-proxy-for-aws` via `uvx`, `@google-cloud/cloud-run-mcp` and `@azure/mcp` via `npx`.
- **The vendor CLI for each cloud you target**: `aws`, `gcloud`, or `az`, authenticated. The agent
  drives these directly for anything its MCP server cannot create.
- **`sudo`**, once, for the microVM installer.

## Install

### Linux (full: analysis + live benchmark)

One command takes a clean machine to a machine that can run the benchmark:

```bash
git clone https://github.com/milosz94/agent-cloud-speed.git
cd agent-cloud-speed
bash setup.sh --check                  # report what is missing, install NOTHING
bash setup.sh --adapter aws            # install it all, then build the substrate
```

`setup.sh` installs whatever is missing and skips whatever is not: curl/git/e2fsprogs, a container
runtime, Node 22 (for `npx`), uv (for `uvx`), the Claude Code CLI, acspeed itself, the per-cloud
config templates, the Firecracker binary + kernel + rootfs, and finally KVM and the tap pool. The
last step configures kernel modules, systemd and host networking, so it uses `sudo` and says so
before it does. `--slots N` sets how many runs can go at once (default 8).

Verify when it finishes:

```bash
python3 -c "import sys;sys.path.insert(0,'.');from autorun import sandbox_available;print(sandbox_available())"
# expect (True, '')
```

The individual steps are documented under "Setup for live runs" below if you would rather run them
by hand.

### macOS (analysis only)

```bash
git clone https://github.com/milosz94/agent-cloud-speed.git
cd agent-cloud-speed
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m unittest discover -s tests -t . -v
```

`acspeed-run` will refuse here. For live runs, use a Linux VM with nested virtualization (UTM,
Lima, or a cloud VM) and follow the Linux instructions inside it.

### Windows (analysis natively; live benchmark via WSL2)

Analysis, in PowerShell:

```powershell
git clone https://github.com/milosz94/agent-cloud-speed.git
cd agent-cloud-speed
py -3 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e .
py -m unittest discover -s tests -t . -v
```

For the live benchmark, install WSL2 with a Linux distribution, confirm `/dev/kvm` exists inside it
(WSL2 exposes KVM on recent Windows builds with nested virtualization enabled), then follow the
Linux instructions **inside WSL2**. Clone into the WSL filesystem, not `/mnt/c`, or the microVM and
file copies will be slow.

## Quick start (analysis, any OS)

```bash
python -m unittest discover -s tests -t . -v      # run the test suite
python examples/run_example.py                    # worked example
python -m acspeed agent-time examples/example_trace.json
python -m acspeed critical-path examples/example_trace.json
python -m acspeed --help                          # agent-time | critical-path | sessions
```

## Where acspeed keeps its config

The per-adapter MCP configs, the frozen reference machine and the pinned STREAM source live in a
data directory, resolved as `$ACSPEED_DATA`, or `~/.acspeed` when that is unset.

**The repo ships working templates.** Copy them once:

```bash
mkdir -p ~/.acspeed/_config
cp -r config/* ~/.acspeed/_config/
```

Then edit the one for your cloud (`gcp.mcp.json` needs your project id; `aws.mcp.json` needs your
IAM profile name). `config/README.md` says what to change in each, and `acspeed-run` refuses to
start, **before provisioning anything**, if the config for your adapter is missing.

## Repo layout

```
acspeed/        the package: traces, splits, floors, adapters, redaction
autorun.py      the live benchmark runner (`acspeed-run`)
build_tables.py table generation used by autorun at the end of a run
config/         MCP + reference templates to copy into $ACSPEED_DATA/_config
examples/       a worked example and a sample trace
results/        the published 94-run artifact: per-cell records, transcripts, exclusion ledger
tests/          the test suite (746 tests, standard library only)
tools/          author-only analysis scripts, not needed to run the benchmark
docs/notes/     working notes kept for provenance, not documentation
```

Everything in `tools/` reads `$ACSPEED_STAGING` for raw run records; those are **not** part of the
published artifact, so a third party regenerates tables from their own runs, not from this repo's.
`docs/notes/REPRO-TODO.md` records what that costs.

## Run the speed test on your own app (`acspeed-run`)

Linux only, and it spends real money on the cloud you point it at.

```bash
pip install -e .                     # gives you `acspeed-run` (and `acspeed`); pipx also works
export ACSPEED_DATA=~/.acspeed       # where the per-cloud MCP configs live (see below)
cd /path/to/your/app                 # any folder an agent could deploy: a repo, a compose stack
acspeed-run --adapter aws --model claude-opus-5
```

One run = the two operations of the paper's Part 2 typology, both performed by a fresh headless
agent on a copy of your folder: **provision** (deploy it), then **deprovision** (remove THAT
deployment). The tool never inspects, manages, or deletes anything else on your account. Results
are **grouped by cloud**: they land in `./acspeed-results/<adapter>/` (`run01.json`, `tables.txt`,
`tables.json`), so `redu`, `aws`, `gcp`, `azure` runs for the same app sit side by side and compare
directly. `--out` overrides the path.

## Setup for live runs (microVM substrate, clouds, concurrency)

`acspeed-run` runs each agent turn inside a **fresh Firecracker microVM** (the paper's C9 hermetic
substrate): the VM holds only the target cloud's credentials, readiness is polled from the host (a
neutral vantage), and the transcript is byte-identical to a plain `claude -p` run. Setup is one-time.

**Platform support.** Linux + KVM only, as "What runs where" above explains. The one-time installer
refuses on any other OS and says so.

**0. Build the microVM images (once per machine).** The Firecracker binary, the kernel and the agent
rootfs are too large for git, so the repo ships the recipe rather than the artifacts:

```bash
bash sandbox/build-images.sh          # ~1 GB of downloads + a container build; idempotent
```

It fetches Firecracker (pinned, default v1.16.1) and a 6.1 CI kernel, then builds `images/rootfs.ext4`
from `rootfs/Containerfile`. It needs `curl`, `tar`, `mke2fs` and either docker or podman, skips
anything already present, and takes `--force` to rebuild. Override `FC_VERSION`, `KERNEL` or
`KERNEL_URL` to pin different versions.

**1. The microVM substrate (once per machine, right after the images).** KVM and the tap pool are ephemeral
kernel state (wiped on reboot), so instead of setting them up by hand every boot, run the one-time
installer, which makes them **persist across reboots** (loads KVM on boot via `modules-load.d`, and
installs a systemd oneshot that brings the tap pool up on boot) and brings everything up now:

```bash
sudo bash sandbox/install-sandbox.sh 8     # 8 = concurrent slots; idempotent, re-run to change it
```

After this you never touch `net-setup` again: KVM auto-loads and `acspeed-tap0..N` come up on every boot.
(The lower-level `sudo modprobe kvm_amd` + `sudo bash sandbox/net-setup.sh N` still work if you want a
one-off, non-persistent setup.) The prebuilt kernel and rootfs live under `sandbox/` (`bin/`, `images/`);
rebuilding the rootfs (only if you edit `sandbox/rootfs/vm-runner.sh` or the `Containerfile`) is documented
in `sandbox/STATE.md`. If the substrate is not available the run **refuses to start** rather than
falling back to the host, and `--no-sandbox` produces the same refusal: a host run yields no
platform/agent split, no session id and no cost, so it would spend real money for an unpublishable
record.

**2. Concurrency (run multiple speed tests at the same time).** With `SLOTS` taps up, just launch up to
`SLOTS` `acspeed-run` processes at once: each **claims a free tap slot** (a file-lock held for the VM's
life) and **queues** if all slots are busy, so nothing collides. Change the count by re-running
`sudo bash sandbox/install-sandbox.sh <N>`. One caveat: two runs of the **same app + same adapter** write
to the same `acspeed-results/<adapter>/` folder, so give one of them `--out` (or run different apps/clouds).

**3. Per-cloud credentials.** Each adapter needs its own reachable cloud, wired in the acspeed data dir
(`$DATA/_config/*.mcp.json`, where `$DATA` is set near the top of `autorun.py`):

- **redu** (`redu.mcp.json`) points at the redu MCP; the microVM keeps your redu login token so the
  agent can deploy, and nothing else.
- **aws** (`aws.mcp.json`) runs `mcp-proxy-for-aws` so the agent drives AWS through `call_aws`. Use a
  **static IAM key**, NOT `aws login`: an `aws login` session expires after a few hours, and if it lapses
  mid-run the deprovision agent has no credentials and leaves a **live, billing orphan**. A static key
  never expires, so deploy and deprovision always authenticate.

  **AWS static-key setup (once).** The repo does this for you:

  ```bash
  bash scripts/aws-bootstrap-credentials.sh --dry-run   # show what it would create
  bash scripts/aws-bootstrap-credentials.sh             # create it
  ```

  It creates the IAM user, attaches AdministratorAccess, mints a non-expiring key, writes the
  `acspeed-batch` profile and verifies it. It is idempotent and refuses if your shell has no admin
  credentials for the target account, since it cannot bootstrap itself. `setup.sh --adapter aws`
  checks whether the profile authenticates and points here when it does not. It is a separate,
  deliberate command rather than part of `setup.sh` because it creates an admin IAM user in your
  account. `config/aws.mcp.json` already passes `--profile acspeed-batch`, so nothing else to wire.

  The equivalent by hand:
  ```bash
  # a dedicated benchmark IAM user + a non-expiring key, written straight into a named profile
  U=acspeed-batch
  aws iam create-user --user-name "$U" --tags Key=purpose,Value=acspeed-benchmark
  aws iam attach-user-policy --user-name "$U" --policy-arn arn:aws:iam::aws:policy/AdministratorAccess
  CRED=$(aws iam create-access-key --user-name "$U" --output json)
  aws configure set aws_access_key_id     "$(echo "$CRED" | python3 -c 'import sys,json;print(json.load(sys.stdin)["AccessKey"]["AccessKeyId"])')"     --profile "$U"
  aws configure set aws_secret_access_key "$(echo "$CRED" | python3 -c 'import sys,json;print(json.load(sys.stdin)["AccessKey"]["SecretAccessKey"])')" --profile "$U"
  aws configure set region us-east-1 --profile "$U"
  unset CRED
  aws sts get-caller-identity --profile "$U"      # expect ...:user/acspeed-batch (a fresh key may need a few seconds)
  ```
  Then wire the MCP config to sign with that profile: `aws.mcp.json` -> add `"--profile", "acspeed-batch"`
  to the `mcp-proxy-for-aws` args. AdministratorAccess is the pragmatic choice for a **dedicated** account
  the agent uses to deploy arbitrary services; keep the key safe (it is admin on that account).

  Before each AWS run, acspeed runs a **credential preflight** (`sts get-caller-identity` with that same
  profile) and **refuses to deploy** if it fails, so a misconfigured profile stops the run loudly instead
  of burning a deploy it cannot tear down.

- **gcp** (`gcp.mcp.json`) runs `@google-cloud/cloud-run-mcp` (the one official create-capable GCP MCP,
  Cloud Run only; Compute Engine / GKE / App Engine go through `gcloud` over Bash). Auth = Application
  Default Credentials: `gcloud auth login` + `gcloud config set project <id>` (or set
  `GOOGLE_APPLICATION_CREDENTIALS` to a service-account key for a batch run), and put the project id into
  `gcp.mcp.json` (`GOOGLE_CLOUD_PROJECT`). The Cloud Run MCP has **no delete tool**, so teardown is
  `gcloud run services delete` (the agent runs it). Cost: a **Cloud Run** deploy is priced as a per-usage
  schedule from the Cloud Billing Catalog via your **ADC** (no separate key) once the (free, read-only)
  Billing API is enabled: `gcloud services enable cloudbilling.googleapis.com` (one-time). Preflight =
  `gcloud auth print-access-token`.

- **azure** (`azure.mcp.json`) runs `@azure/mcp` (`azmcp`); native compute/appservice tools are mostly
  read/query, so the create/deploy/teardown path is the `extension` namespace running `az`/`azd`
  (`az containerapp up` / `az webapp up` / `az vm create`; `az group delete` to tear down). Auth =
  `az login` (or service-principal env vars `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET`
  for a batch run). Cost: a **Container Apps** deploy is priced as a per-usage schedule from the **public**
  Azure Retail Prices API (no key, no API-enable step; USD-native; live-verified). Preflight = `az account show`.

  For both **gcp** and **azure**, the credential preflight (`gcloud auth print-access-token` / `az account
  show`) runs before the deploy and **refuses to deploy** if it fails, exactly as for AWS.

  **microVM note (gcp/azure):** the hermetic microVM mounts `~/.config/gcloud` / `~/.azure` only after the
  rootfs is rebuilt to pick up the generalized `vm-runner` (the host-side staging and `vm-runner.sh` are
  already generalized; the running rootfs image is not). Until you rebuild it, run gcp/azure with
  `--no-sandbox` (agent turns on the host, using the host's creds directly). AWS and redu microVM runs are
  unaffected.

**3b. Cost coverage: the complete-inventory sweep (one-time per cloud, off-clock).**

To make the cost axis catch **every** billable resource the agent provisioned (not just the app compute
and database it obviously created), each adapter discovers the deploy's whole resource scope from the
cloud's **own complete inventory**, then prices each resource or discloses it by type - so a resource type
the tool has no explicit price for is surfaced, never silently counted as $0, and a new type is caught with
no code change. This sweep is off-clock (it never affects the measured deploy time) and best-effort: if the
inventory API below is not enabled it no-ops and the run says so. Enable it once per cloud:

- **gcp** - enable the **Cloud Asset API** (free, read-only): `gcloud services enable
  cloudasset.googleapis.com` on the project. The sweep runs `gcloud asset search-all-resources` scoped to
  the run token. It is on by default; without the API it returns nothing (disclosed). Set
  `ACSPEED_GCP_ASSET_SWEEP=0` to force it off.
- **aws** - create an **AWS Resource Explorer** index (not tag-gated, so it finds untagged / auto-created
  resources like public IPv4 that the tag inventory misses): `aws resource-explorer-2 create-index`, then
  for cross-region coverage promote it with `aws resource-explorer-2 update-index-type --arn <index-arn>
  --type AGGREGATOR`. Without an index the sweep falls back to the tag inventory, then AWS Config
  (disclosed).
- **azure** - no API to enable: each run deploys into its **own** resource group `rg-acs<token>`, and the
  sweep lists it with `az resource list -g rg-acs<token>` (built-in). It needs only the same `az login`
  the deploy already uses.

**4. Run it.**

```bash
acspeed-run --adapter redu  --model claude-opus-5     # time + liveness + cost + capability
acspeed-run --adapter aws   --model claude-opus-5     # same, on AWS (uses the static-key profile)
acspeed-run --adapter azure --model claude-opus-5 --no-sandbox   # Azure (host creds; cost is public-priced)
acspeed-run --adapter gcp   --model claude-opus-5 --no-sandbox   # GCP  (host creds; Cloud Run + gcloud)
# --no-cost / --no-capability skip those off-clock measures; --n N repeats the run
```

**The clock, pinned.** `t0` = the deploy request. `t1` = the first response the deployed URL gives
with HTTP status **< 500**, found by an **external poller running concurrently** with the agent
(it tails the session transcript for the URL and polls it every 5 s). 000 (no TLS/TCP) and 5xx
are "not serving"; anything the application itself answers, 3xx and 4xx included, is serving:
Isso answers 400 at `/`, Umami redirects to `/login`, an API 401s. The agent's own "done" is never
the boundary, and agent work after `t1` (verification, notes) is outside the operation: the trace
is clipped at `t1`. First passing poll, N=1. This is the SRE "not 5xx" form, a disclosed choice
(see `PAPER/CHANGE-REQUESTS.md` C1+C2 in the paper repo); the Kubernetes 200-399 form applies only
where the task carries an app-declared health path. Only a URL that never serves within the budget
triggers a repair prompt.

**Optional: a screenshot of the working app.** Add `--screenshot` and, once the app is live and
before it is torn down, the tool saves a full-page picture to `run<NN>.png`. This is off-clock and
not part of the paper: it runs after `t1`, never affects timing, and sends no agent prompt. It uses
Playwright if installed, otherwise a headless chromium/chrome on your PATH, otherwise it skips with
a hint. It is a keepsake / proof artifact, nothing the measurement depends on.

## What maps to what (paper Parts 1-4)

| Module | Paper section | Implements |
|---|---|---|
| `acspeed/criticalpath.py` | Section 2 (the spine) | Build the dependency DAG, longest path = wall-clock, slack, and the **owner split**: `wall-clock = critical-platform + critical-agent`, `overlap = raw - critical`. Method reused from the critical-path method (Kelley and Walker 1959; Blumofe and Leiserson 1999) and trace-based critical-path analysis (The Mystery Machine, OSDI 2014; CRISP, USENIX ATC 2022). |
| `acspeed/capability.py` | Section 3 (platform) | Reference-ratio normalization `r = measured/reference` and the delivered-capability index `DCI` (weighted geometric mean; Fleming and Wallace 1986); dominant axis by measurement (USE method; Roofline). |
| `acspeed/discriminator.py` | Section 3 (platform) | The fixed-plus-variable fit `T(rate) = t_fixed + W/rate`: intercept = control-plane floor, slope = data-plane work (Hockney; LogP; Amdahl; Mao and Humphrey 2012). Plus the Karp-Flatt serial-fraction falsifier (CACM 1990). |
| `acspeed/agenttime.py` | Section 4 (agent) | Raw vs critical agent-time and the component split (inference / orchestration / wait / rework). Inference seconds `= TTFT + TPOT * (output_tokens - 1)` (MLPerf; TTFT already prices the first token). |
| `acspeed/repro.py` | Section 6 | Geometric mean, bootstrap and normal CIs, the CONFIRM repeat-until-tight rule (Maricq et al. 2018), and the non-overlapping-CI comparison rule. |
| `acspeed/probes.py` | Section 3 (platform) | Parsers for the delivered-capability probes: `sysbench` (compute), STREAM (memory), `fio` (disk), and the network axis: **VM-to-VM** `iperf3` throughput + `ping` RTT between two of the operation's own instances over the tenant private network (C17), a scored axis only for multi-VM operations (single-VM: N/A + nominal NIC disclosed, C18). Runner wrappers that shell out live in `runners.py`. |
| `acspeed/operation.py` | Part 2, Sections 2-4, 6 | The operation as a **composite over atoms**: the seven-slot schema, the three-type typology (provision / operate-mutate / deprovision) via a measured `profile()`, the three atom registers with the platform/agent invariant, and the **milestone split** that reuses SSH-ready to separate the cited lower half from the novel upper-half agent/platform decomposition. Executable falsifiability checks (`is_schema_conformant`). |
| `acspeed/session.py` | Part 2, Section 7 | Session as a **trace of operations**, and `efficiency(actual, optimal)` = excess critical-path wall-clock vs an optimal reference trace, with the exact identity `excess = selection_excess + execution_excess`. Constructing the reference is Part 3 (`reference.py`). |
| `acspeed/reference.py` | Part 3, Sections 1-4 | The **operation-state graph** and its floor cost-to-go `V_F`; the reference-optimal floor makespan `F_C = V_F(start)` as a shortest cost-to-go; the exact decomposition of **excess over the floor** into per-decision advantages (telescoping to `M_actual - F_C`) and the floor-twin **selection vs execution** split; the two-ratio bracket `F_C <= optimum <= best-achieved` (structural floor + DEA frontier) and the refutable gold. The decomposition is against the **floor**, not a given optimum, per the Part 3 rework. |
| `acspeed/weighting.py` | Part 4 (weighting) | Aggregation with **no chosen weight**: `suite_total` (the total-time headline `E_X`), `geomean_ratio` (the reference-invariant companion `G_X`; Fleming and Wallace 1986), `suite_verdict` (report the ranking only when both agree), `leave_one_out` (the drop-any-task sensitivity), and the matched-block per-task `more_efficient` (non-overlapping CIs). The `(wall-clock, resource-cost)` **cost-performance frontier** under the Pareto-Koopmans criterion (`dominates`, `pareto_frontier`, `classify_run`, `is_tradeoff`): a run that spends more cost for no less time carries a positive cost slack and is dominated, so speed cannot be bought (Pareto 1896; DEA, Charnes-Cooper-Rhodes 1978; Banker-Charnes-Cooper 1984). |
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

## Sessions (auditable evidence)

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

## License

MIT (see `LICENSE`). Fill in the copyright holder before publishing.
