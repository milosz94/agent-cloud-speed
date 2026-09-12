# Config templates

`acspeed-run` reads its per-cloud setup from a data directory, resolved as `$ACSPEED_DATA`, or
`~/.acspeed` when that is unset. Copy this folder there once:

```bash
mkdir -p ~/.acspeed/_config
cp -r config/* ~/.acspeed/_config/
```

Then edit the file for the cloud you intend to run. `acspeed-run` refuses to start, before
provisioning anything, if the config for your adapter is missing.

| file | what to change |
|---|---|
| `aws.mcp.json` | the `--profile` name, if yours is not `acspeed-batch`. Use a static IAM key, not `aws login`: an expiring session that lapses mid-run leaves a billing orphan. |
| `gcp.mcp.json` | `GOOGLE_CLOUD_PROJECT` (required) and `GOOGLE_CLOUD_REGION`. |
| `azure.mcp.json` | nothing, usually. Auth comes from `az login` or the service-principal environment variables. |
| `redu.mcp.json` | nothing. |
| `reference_machine.json` | the frozen reference vector the capability probe normalizes against. Replace it with your own machine's if you want your own reference; the shipped one is the author's. |
| `stream.c` | McCalpin STREAM 5.10, pinned. Leave it alone. |
| `example.workload.json` | an example request mix for `tools/probe_loadtest.py`. |

What each cloud needs beyond the MCP config (credentials, preflight, teardown) is under "Per-cloud
credentials" in the top-level README.
