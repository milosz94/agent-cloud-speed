# Contributing

Contributions are welcome, and three kinds especially.

## Add your cloud

If you run a cloud and it is not here, add an adapter. You know your platform better than anyone
benchmarking it from outside, and an adapter written by the people who built the thing is a better
measurement than one reverse-engineered from docs.

An adapter is a `CloudProfile`: a mapping from four canonical operations (`provision`, `wait_ready`,
`status`, `teardown`) onto your MCP server's tool names, plus a page saying what a user must supply.
See [`acspeed/adapters/profiles.py`](acspeed/adapters/profiles.py) and
[`docs/adapters/`](docs/adapters/). The analysis is identical across clouds; only the profile
differs, so this is usually a small change.

## Correct your own numbers

**If you work at AWS, Google or Microsoft: the adapter for your cloud is yours to correct.** These
were written from the outside, against your public MCP servers and CLIs, and the agent's path
through them is exactly the thing being measured. If it takes a route your team would not recommend,
that is a finding about the measurement, not about your cloud, and a PR fixing it is worth more than
an argument about the result.

Concretely, the useful corrections are: a better tool mapping, a create or delete path we fell back
to the CLI for because the MCP server could not do it, an authentication setup that is more standard
than the one documented, or a pricing component the cost axis prices wrongly. Numbers in
[`results/`](results/) come with the transcript of every run, so any claim about what the agent did
is checkable rather than a matter of opinion.

The one thing a PR cannot do is change a published number without the run that produced it. New
numbers need new runs, with their transcripts.

## Publish a benchmark

The suite here deploys one app. It is not special. If you have a workload that exercises something
this one does not, contribute it: a benchmark is a JSON file describing operations and the URL reads
that verify them, and [docs/writing-a-benchmark.md](docs/writing-a-benchmark.md) is the whole story.
Python suites are also welcome when a check needs real logic.

A benchmark is more useful with a run behind it. Say which cloud, which agent and which model, and
include the transcript.

## Practical notes

- `python3 -m unittest discover -s tests` before opening a PR. It needs no cloud account and no
  network.
- A rule that can be checked mechanically should arrive as a test, not as a paragraph in a document.
- No em dashes or en dashes in tracked files; a test enforces it.
- Live runs cost real money on a real account, so nothing in CI runs one. If a change affects the
  live path, say what you ran and paste what came back.
- macOS and Windows are experimental: the QEMU backend is proven end to end on Linux only. If you
  complete a run on either, that report is genuinely valuable, including the boot log.

## Reporting a problem

A useful report says what you ran, what came back, and which cloud and agent. For a failed live run
the boot log and the agent's returned result are the two things that matter. For a wrong number, the
run record and its transcript.
