# GCP adapter

## You must do (not automatable)

1. **A GCP project you can deploy into**, with billing enabled on it.
2. **Authenticate**, so the agent can deploy *and* tear down:
   ```bash
   gcloud auth login
   gcloud config set project <YOUR_PROJECT_ID>
   ```
   For an unattended batch, use a service account instead and point
   `GOOGLE_APPLICATION_CREDENTIALS` at its key file.
3. **Enable the Billing API once**, or the cost axis cannot price a run:
   ```bash
   gcloud services enable cloudbilling.googleapis.com
   ```
   It is free and read-only. Without it the run still measures time, but reports no cost.
That is all. There is no config file to edit: acspeed writes its own on first use and takes the
project id from the `gcloud` config you just set. If you want a region other than `europe-west1`,
set `GOOGLE_CLOUD_REGION` in the environment, or edit the generated file at
`~/.acspeed/_config/gcp.mcp.json` once it exists.

## The repo does

- writes `~/.acspeed/_config/gcp.mcp.json` on first use, filling the project id from
  `GOOGLE_CLOUD_PROJECT` or `gcloud config get-value project`
  (`autorun.py::ensure_adapter_config`, `tests/test_config_auto.py`)
- checks `gcloud auth print-access-token` and prints the fix when it fails
  (`setup.sh --check --adapter gcp`), the same check the run itself makes
- refuses before provisioning, naming the one command to run, only if no project id can be found
  anywhere

## Known shape of this adapter

The MCP server is `@google-cloud/cloud-run-mcp`, the one official create-capable GCP MCP, and it
covers **Cloud Run only**. Compute Engine, GKE and App Engine go through `gcloud` over Bash, which
the agent drives itself. The server has **no delete tool**, so teardown is
`gcloud run services delete`, which the agent runs. Cost is a per-usage schedule read from the Cloud
Billing Catalog through your own credentials, so no separate key is needed.
