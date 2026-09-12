# Per-adapter setup

One file per cloud. Each lists what you must do yourself (an account, a login, an id only you know)
and what the repo does for you. `setup.sh --check --adapter <cloud>` reports the same list live.

| adapter | you supply | repo does |
|---|---|---|
| [aws](aws.md) | an AWS account you are admin of | creates the benchmark IAM user and key, pre-wires the profile |
| [gcp](gcp.md) | a GCP project, `gcloud` login, the project id | ships the MCP template, checks credentials, refuses on an unedited template |
| [azure](azure.md) | an Azure subscription, `az login` | ships the MCP template, checks credentials |
| [redu](redu.md) | a redu account | ships the MCP template (HTTP, no CLI) |

**What is never automatable:** owning a cloud account, and an interactive login. Everything after
that, the repo does.
