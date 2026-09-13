# acspeed

Benchmarks how fast a cloud is to operate when an AI coding agent is driving it.

An operation is any infrastructure change an agent performs on your behalf: provisioning something,
changing something that is already running, or tearing it down. Deploying an app is one operation
of several. acspeed gives a headless agent a real app, a real cloud account and a task, and times
what happens. Same app, same agent, same task on every cloud, so the clouds are comparable.

Every run is live, against a real account, and costs real money. There is no offline mode.

```bash
git clone https://github.com/milosz94/agent-cloud-speed.git && cd agent-cloud-speed
bash setup.sh                            # asks which clouds to set up
cd /path/to/your/app
acspeed-run --adapter <cloud> --model claude-opus-5
```

## Requirements

| | |
|---|---|
| OS | **Linux** (verified). macOS and Windows are **experimental** |
| Python | 3.10+, no third-party dependencies |
| Agent | Claude Code or Codex, installed and logged in (`--agent claude\|codex`) |
| Node.js + uv | runs the per-cloud MCP servers (`npx`, `uvx`) |
| Cloud access | an account on the cloud you target, logged in: `aws`, `gcloud` or `az` for those three; redu needs only an account token, no CLI |
| sudo | once, for the microVM installer |

`bash setup.sh --check` reports what is missing and changes nothing.
Full detail in [docs/install.md](docs/install.md).

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

## Results

One cell, to show what comes out. Medium A: deploy umami with a managed database, then register a
user, deploy a second site, wire analytics between them, and survive a restart. Same app, same
agent, same task on each cloud, n=10 per cloud.

| cloud | wall-clock (s) | 95% CI | cloud was busy | agent was busy | neither |
|---|--:|---|--:|--:|--:|
| GCP | **1,793** | [1,335, 2,439] | 944 | 697 | 151 |
| Azure | 2,578 | [2,180, 2,946] | 1,904 | 673 | 1 |
| AWS | 4,124 | [3,324, 5,071] | 2,578 | 1,314 | 232 |

The split is why this is worth measuring. AWS is slowest here, and not for one reason: its platform
time is the largest *and* its agent spent 1,314 s against Azure's 673 s on the same task. Azure is
74% platform, so a faster agent would barely move its total; GCP is 53%, where the agent is worth
attacking. Those are different problems, and a single number does not tell you which one you have.

Full tables, per-run rows and the transcript behind every row:

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

## Contributing

Adapters for new clouds, corrections to the existing ones from the people who run them, and new
benchmarks are all welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
