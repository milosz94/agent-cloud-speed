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
4. **Put your project id in the MCP config**, because only you know it:
   ```bash
   $EDITOR $ACSPEED_DATA/_config/gcp.mcp.json     # or ~/.acspeed/_config/gcp.mcp.json
   ```
   Replace `REPLACE_WITH_YOUR_GCP_PROJECT_ID` with the project, and set `GOOGLE_CLOUD_REGION` if
   you do not want `europe-west1`. **A run refuses to start while that placeholder is still there**,
   so an unedited template cannot reach the cloud.

## The repo does

- ships `config/gcp.mcp.json` and copies it into place (`setup.sh`)
- checks `gcloud auth print-access-token` and prints the fix when it fails
  (`setup.sh --check --adapter gcp`), the same check the run itself makes
- refuses before provisioning if the config is missing or still a template

## Known shape of this adapter

The MCP server is `@google-cloud/cloud-run-mcp`, the one official create-capable GCP MCP, and it
covers **Cloud Run only**. Compute Engine, GKE and App Engine go through `gcloud` over Bash, which
the agent drives itself. The server has **no delete tool**, so teardown is
`gcloud run services delete`, which the agent runs. Cost is a per-usage schedule read from the Cloud
Billing Catalog through your own credentials, so no separate key is needed.
