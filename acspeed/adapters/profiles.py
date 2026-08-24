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

# Hyperscaler stubs. Replace the TODO tool names with the target MCP server's
# actual tool names (and confirm whether it offers a blocking readiness tool or
# only status polling) when wiring Part 5.
AWS = CloudProfile(
    name="aws",
    tool_map={"provision": "TODO_run_instances", "wait_ready": "TODO_wait",
              "status": "TODO_describe_instances", "teardown": "TODO_terminate_instances"},
    readiness="poll",
)
GCP = CloudProfile(
    name="gcp",
    tool_map={"provision": "TODO_instances_insert", "wait_ready": "TODO_wait",
              "status": "TODO_instances_get", "teardown": "TODO_instances_delete"},
    readiness="poll",
)
AZURE = CloudProfile(
    name="azure",
    tool_map={"provision": "TODO_vm_create", "wait_ready": "TODO_wait",
              "status": "TODO_vm_get", "teardown": "TODO_vm_delete"},
    readiness="poll",
)

PROFILES = {"redu": REDU, "aws": AWS, "gcp": GCP, "azure": AZURE}
