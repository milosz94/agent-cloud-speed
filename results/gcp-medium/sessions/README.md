# Sessions (credential-stripped, infrastructure-neutral)

These are the raw agent deploy/measurement sessions behind the paper's numbers and its falsifiability
claims, redacted so they can ship as auditable evidence: they show what the agent DID, the app it
deployed and the timing, not any secret or how the cloud is built underneath.

Removed: generated app secrets (passwords, API tokens, encryption keys), any cloud OAuth bearer/JWT,
private-key and SSH-key material, connection-string passwords; and the infrastructure setup -- provider
technology names, control-plane hostnames and internal IP addresses. Kept: the agent's tool calls, the
deployed app URLs, and the platform brand.

Redaction touched only secret- or substrate-bearing string values: timestamps, token counts, tool
structure and message shape are unchanged, so each session still reconstructs the exact owner-labelled
trace and token totals the reference core computes (see acspeed/transcript.py). Placeholders read
``[REDACTED:...]`` (secrets), ``[infra]`` / ``[infra-host]`` (setup) and ``[ip]``. See
REDACTION-MANIFEST.json for per-file counts; the bundle was verified to contain no residual secret- or
infrastructure-shaped strings.
