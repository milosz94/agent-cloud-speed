# acspeed

Benchmarks how fast a cloud is to operate when an AI coding agent is driving it.

An operation is any infrastructure change an agent performs on your behalf: provisioning something,
changing something that is already running, or tearing it down. Deploying an app is one operation
of several. acspeed gives a headless agent a real app, a real cloud account and a task, and times
what happens. Same app, same agent, same task on every cloud, so the clouds are comparable.

Every run is live, against a real account, and costs real money. There is no offline mode.

```bash
git clone https://github.com/milosz94/agent-cloud-speed.git && cd agent-cloud-speed
bash setup.sh --adapter <cloud>          # aws | gcp | azure | redu
cd /path/to/your/app
acspeed-run --adapter <cloud> --model claude-opus-5
```

## Requirements

| | |
|---|---|
| OS | **Linux** (verified). **macOS and Windows are experimental**: the backend is there and untested, see [docs/install.md](docs/install.md#macos-and-windows-experimental) |
| Python | 3.10+, no third-party dependencies |
| Agent | Claude Code or Codex, installed and logged in (`--agent claude\|codex`) |
| Node.js + uv | runs the per-cloud MCP servers (`npx`, `uvx`) |
| Cloud access | an account on the cloud you target, logged in: `aws`, `gcloud` or `az` for those three; redu needs only an account token, no CLI |
| sudo | once, for the microVM installer |

`bash setup.sh --check --adapter <cloud>` reports what is missing and changes nothing.

Every agent turn runs in a fresh microVM. On Linux that is Firecracker, which needs `/dev/kvm`.
macOS and Windows use QEMU with the hypervisor the OS already provides (Hypervisor.framework, WHPX),
which needs no nested virtualization. **Those two are experimental**: the QEMU backend is proven end
to end on Linux only, so a run there prints a warning, records `experimental: true`, and is never
pooled with verified runs. Full detail in [docs/install.md](docs/install.md).

## Usage

```bash
cd /path/to/your/app
acspeed-run --adapter <cloud> --model claude-opus-5
```

The agent works on a copy of your folder and only touches what it creates on your account. Results
land in `./acspeed-results/<adapter>/`, so several clouds for the same app sit side by side.

| flag | effect |
|---|---|
| `--adapter` | which cloud: `aws`, `gcp`, `azure`, `redu` |
| `--agent` | which agent CLI drives the run: `claude` (default) or `codex` |
| `--model` | a model string your account can use |
| `--suite` | run a built-in benchmark tier instead of a plain deploy |
| `--custom` | run your own benchmark from a JSON file ([how](docs/writing-a-benchmark.md)) |
| `--n` | repeat count |
| `--out` | override the results path |

| output | contents |
|---|---|
| `run01.json` | the run: every timed leg, the cost, the session ids |
| `tables.txt` | the same run as a readable table |
| `tables.json` | the same run for a machine |

## Reproduce a published cell

The application under test ships with the repo and is pinned by image digest, which is what makes a
re-run comparable with a published row:

```bash
mkdir -p ~/acspeed-medium-a && cd ~/acspeed-medium-a
acspeed-run --adapter aws --suite umami-medium --model claude-opus-5 --n 10
```

`--suite umami-medium` is the Medium A (online) tier, the same one behind `results/aws/aws-medium-a/`.
The app folder is created for you on first run. Compare your `tables.txt` against that cell's
`README.md`.

**It costs real money**, on your own cloud account, and a Medium cell is ten deploys plus their
operations and teardown.

## Results

| where | what |
|---|---|
| `./acspeed-results/<adapter>/` | your own runs |
| [`results/`](results/) | the published runs, one folder per cloud and cell |
| [`results/README.md`](results/README.md) | what a cell is, how to read the tables |
| [`results/PLAYBOOK.md`](results/PLAYBOOK.md) | how a run becomes a published row |

Every published number traces back to a redacted transcript in the same folder.

## Clouds

| adapter | you need | setup |
|---|---|---|
| aws | an AWS account you administer | [docs/adapters/aws.md](docs/adapters/aws.md) |
| gcp | a GCP project and `gcloud` login | [docs/adapters/gcp.md](docs/adapters/gcp.md) |
| azure | an Azure subscription and `az login` | [docs/adapters/azure.md](docs/adapters/azure.md) |
| redu | a redu account token | [docs/adapters/redu.md](docs/adapters/redu.md) |

The agent drives each cloud through that cloud's MCP server. Adding a cloud is a new profile, not a
new code path.

## Docs

| page | what is in it |
|---|---|
| [docs/install.md](docs/install.md) | install, the microVM substrate, concurrency, credentials |
| [docs/measurement.md](docs/measurement.md) | what is measured, how it is defined, limits |
| [docs/writing-a-benchmark.md](docs/writing-a-benchmark.md) | benchmark your own app: operations and checks in JSON |
| [docs/adapters/](docs/adapters/) | one page per cloud |
| [results/](results/) | the published runs and their transcripts |

## Paper

acspeed is the reference implementation for *A Reproducible, Cloud-Agnostic Baseline for Measuring
Agent-Cloud Operation Efficiency*, which defines the measurement and reports the study behind
[`results/`](results/). Not yet published; the link and citation land here when it is.

## License

See [LICENSE](LICENSE).
