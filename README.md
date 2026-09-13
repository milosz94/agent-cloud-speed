# acspeed

Benchmarks how fast a cloud is to deploy to when an AI coding agent is doing the deploying.

It gives a headless agent a real app, a real cloud account and a task, times the run end to end, and
splits the elapsed time into the part the cloud spent and the part the agent spent. Same app, same
agent, same task on AWS, GCP and Azure, so the clouds are comparable.

Every run is a live run against a real account and costs real money. There is no offline mode.

```bash
git clone https://github.com/milosz94/agent-cloud-speed.git && cd agent-cloud-speed
bash setup.sh --adapter aws
cd /path/to/your/app && acspeed-run --adapter aws --model claude-opus-5
```

## Requirements

| | |
|---|---|
| OS | Linux with KVM: `ls /dev/kvm` must succeed |
| Python | 3.10+, no third-party dependencies |
| Agent | Claude Code, installed and logged in |
| Node.js + uv | runs the per-cloud MCP servers (`npx`, `uvx`) |
| Cloud CLI | `aws`, `gcloud` or `az` for the cloud you target, logged in |
| sudo | once, for the microVM installer |

`bash setup.sh --check --adapter aws` reports what is missing without changing anything.
Every agent turn runs in a fresh Firecracker microVM, which is why KVM is required.
See [docs/install.md](docs/install.md).

## Usage

```bash
cd /path/to/your/app
acspeed-run --adapter aws --model claude-opus-5
```

A run is two operations performed by a fresh agent on a copy of your folder: deploy it, then remove
that deployment. Nothing else on your account is touched. Results land in
`./acspeed-results/<adapter>/`, so several clouds for the same app sit side by side.

| file | contents |
|---|---|
| `run01.json` | the splits, the cost, the session ids, every axis |
| `tables.txt` | the same run as a readable table |
| `tables.json` | the same run for a machine |

| flag | effect |
|---|---|
| `--adapter` | which cloud: `aws`, `gcp`, `azure`, `redu` |
| `--model` | model string your account can use |
| `--suite` | run a benchmark tier instead of a plain deploy |
| `--n` | repeat count |
| `--out` | override the results path |

## Reproduce a published cell

The app under test ships with the repo, pinned by image digest, which is what makes a re-run
comparable with a published row.

```bash
mkdir -p ~/acspeed-medium-a && cd ~/acspeed-medium-a
acspeed-run --adapter aws --suite umami-medium --model claude-opus-5 --n 10
```

That is the cell behind [`results/aws/aws-medium-a/`](results/aws/aws-medium-a/). The app folder is
created on first run. Compare your `tables.txt` against that cell's `README.md`.

Ten deploys plus their operations and teardown, billed to your account.

## Published results

94 runs across nine cells, in [`results/`](results/). Every number traces back to a redacted
transcript in the same folder.

| cloud | easy | medium A | medium B |
|---|--:|--:|--:|
| [aws](results/aws/) | 10 | 10 | 12 |
| [gcp](results/gcp/) | 12 | 10 | 10 |
| [azure](results/azure/) | 10 | 10 | 10 |

| file | contents |
|---|---|
| [`results/README.md`](results/README.md) | what a cell is, how to read the tables |
| [`results/PLAYBOOK.md`](results/PLAYBOOK.md) | how a run becomes a published row |
| [`results/DATA-DEFECTS.md`](results/DATA-DEFECTS.md) | known defects and exclusions |

## Clouds

| adapter | you need | setup |
|---|---|---|
| aws | an AWS account you administer | [docs/adapters/aws.md](docs/adapters/aws.md) |
| gcp | a GCP project and `gcloud` login | [docs/adapters/gcp.md](docs/adapters/gcp.md) |
| azure | an Azure subscription and `az login` | [docs/adapters/azure.md](docs/adapters/azure.md) |
| redu | a redu account | [docs/adapters/redu.md](docs/adapters/redu.md) |

The agent drives each cloud through that cloud's MCP server. Adding a cloud means adding a profile,
not a new code path.

## Docs

| page | what is in it |
|---|---|
| [docs/install.md](docs/install.md) | install, the microVM substrate, concurrency, credentials |
| [docs/measurement.md](docs/measurement.md) | what is measured, how the split is defined, limits |
| [docs/adapters/](docs/adapters/) | one page per cloud |
| [results/](results/) | the published runs and their transcripts |

## Paper

acspeed is the reference implementation for *A Reproducible, Cloud-Agnostic Baseline for Measuring
Agent-Cloud Operation Efficiency*, which defines the measurement and reports the 94-run study.
Not yet published; this section will carry the link and citation once it is.

## License

See [LICENSE](LICENSE).
