# acspeed

Benchmarks how fast a cloud is to operate when an AI coding agent is driving it.

An operation is any infrastructure change an agent performs on your behalf: provisioning something,
changing something that is already running, or tearing it down. Deploying an app is one operation
of several. acspeed gives a headless agent a real app, a real cloud account and a task, and times
what happens. Same app, same agent, same task on every cloud, so the clouds are comparable.

Every run is live, against a real account, and costs real money.

```bash
git clone https://github.com/milosz94/agent-cloud-speed.git && cd agent-cloud-speed
bash setup.sh                            # asks which clouds to set up
cd /path/to/your/app
acspeed-run --adapter <cloud> --model claude-opus-5
```

## Example results

| cloud | run | total | deploy (t1) | register | site-b | integrate | durability | agent $ | tier |
|:--|----:|------:|------------:|---------:|-------:|----------:|-----------:|--------:|:----:|
| aws | [1](results/aws/aws-medium-a/sessions/a0c27b89-6125-4308-821f-95d80c7e31f5.jsonl) | 3060 | 666 | 38 | 1001 | 788 | 567 | $6.93 | 5/5 |
| aws | [2](results/aws/aws-medium-a/sessions/41a44d06-8086-4a00-8f01-8618c43eaaae.jsonl) | 2700 | 750 | 19 | 439 | 1025 | 467 | $11.16 | 5/5 |
| gcp | [1](results/gcp/gcp-medium-a/sessions/97ec285a-4f45-4f9d-b236-e9c54a6fc453.jsonl) | 2428 | 472 | 42 | 368 | 1103 | 443 | $4.79 | 5/5 |
| gcp | [2](results/gcp/gcp-medium-a/sessions/23e283ed-49b6-45a3-a9ee-3410effdd1da.jsonl) | 1113 | 365 | 27 | 201 | 164 | 356 | $3.72 | 5/5 |
| azure | [1](results/azure/azure-medium-a/sessions/b62c17d1-8d4e-48ad-8b8a-1c22c34d0107.jsonl) | 2179 | 1221 | 22 | 346 | 61 | 529 | $4.32 | 5/5 |
| azure | [2](results/azure/azure-medium-a/sessions/9bb69ed8-b98a-4dfa-a529-131e716b3df6.jsonl) | 2827 | 2252 | 32 | 194 | 152 | 197 | $5.12 | 5/5 |

Published results are in [`results/`](results/).

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
