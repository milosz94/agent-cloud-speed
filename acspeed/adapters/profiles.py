"""Per-cloud profiles: map the canonical operations to a cloud's MCP server and
tool names, and declare its readiness style. The analysis is identical across
clouds; only these maps differ.

redu's tool names are the real redu MCP tools. The hyperscaler profiles are
stubs whose tool names are filled in against the target MCP server during the
Part 5 validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class CloudProfile:
    name: str
    tool_map: Dict[str, str]                 # canonical op -> MCP tool name
    readiness: str = "blocking"              # "blocking" | "poll"
    poll_interval: float = 3.0               # seconds, used in poll mode
    server_command: Optional[List[str]] = None  # for a real stdio transport

    def tool(self, canonical: str) -> str:
        if canonical not in self.tool_map:
            raise KeyError(f"profile {self.name!r} has no tool mapped for {canonical!r}")
        return self.tool_map[canonical]


# redu exposes a real blocking readiness tool (wait_for_deployment).
REDU = CloudProfile(
    name="redu",
    tool_map={
        "provision": "create_instance",
        "wait_ready": "wait_for_deployment",
        "status": "get_deployment",
        "teardown": "delete_instance",
    },
    readiness="blocking",
)

# AWS: the deploy path is the aws MCP (mcp-proxy-for-aws, generic call_aws) driven by the agent; these
# low-level canonical names are the EC2 verbs for the (unused-by-the-agent-path) MCPAdapter.run() shape.
AWS = CloudProfile(
    name="aws",
    tool_map={"provision": "run_instances", "wait_ready": "describe_instances",
              "status": "describe_instances", "teardown": "terminate_instances"},
    readiness="poll",
)
# GCP: the one official CREATE-capable MCP is @google-cloud/cloud-run-mcp (Cloud Run only; deploy is
# blocking and returns the serving URL; NO delete tool -> teardown is `gcloud run services delete` over
# Bash). Compute Engine / GKE / App Engine have no official creating MCP -> the GCP route for those is the
# `gcloud` CLI over Bash. Auth = Application Default Credentials (a service-account key for a batch run).
# Verified 2026-08-28: GoogleCloudPlatform/cloud-run-mcp README + npm.
GCP = CloudProfile(
    name="gcp",
    server_command=["npx", "-y", "@google-cloud/cloud-run-mcp"],
    tool_map={"provision": "deploy-local-folder",   # or deploy-file-contents
              "wait_ready": "deploy-local-folder",   # deploy is blocking: returns the URL once serving
              "status": "get-service",
              "teardown": "gcloud run services delete"},  # NOT an MCP tool -> Bash/gcloud fallback
    readiness="blocking",
)
# Azure: the official @azure/mcp (azmcp); its native compute/appservice tools are largely read/query, so
# the dependable CREATE/DEPLOY/TEARDOWN path is the `extension` namespace executing `az`/`azd`
# (az containerapp up / az webapp up / az vm create / azd up ; az group delete for teardown). Auth =
# DefaultAzureCredential (service-principal env vars for a batch run). Verified 2026-08-28: npm @azure/mcp
# + Learn tools index. (LIVE SEAM: the exact `extension` execute-tool name is confirmed on first run;
# the `az`/`azd`-over-Bash path is the guaranteed fallback, same as GCP's gcloud fallback.)
AZURE = CloudProfile(
    name="azure",
    server_command=["npx", "-y", "@azure/mcp@latest", "server", "start"],
    tool_map={"provision": "extension_az",   # az containerapp up / az webapp up / az vm create ; or azd up
              "wait_ready": "extension_az",   # az create is blocking (returns when provisioned)
              "status": "appservice",          # native get/list for App Service; `compute` for VMs
              "teardown": "extension_az"},     # az group delete --yes
    readiness="blocking",
)

PROFILES = {"redu": REDU, "aws": AWS, "gcp": GCP, "azure": AZURE}
