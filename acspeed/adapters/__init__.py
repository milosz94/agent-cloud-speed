"""MCP-based cloud adapters (Part 1, Section 5 / the coupling).

To measure a cloud, point an ``MCPAdapter`` at that cloud's MCP server via a
transport, with the cloud's ``CloudProfile``. The adapter emits a trace that the
analysis core consumes; the interface (MCP tool shape, blocking vs poll
readiness) is exactly the coupling lever the framework measures.
"""
from __future__ import annotations

from . import profiles
from .base import PROVISION, STATUS, TEARDOWN, WAIT_READY, CloudAdapter, OperationSpec
from .mcp_adapter import MCPAdapter
from .mcp_client import MCPClient, StdioTransport
from .mock import MockCloudTransport, VirtualClock
from .profiles import AWS, AZURE, GCP, PROFILES, REDU, CloudProfile

__all__ = [
    "CloudAdapter", "OperationSpec", "PROVISION", "WAIT_READY", "STATUS", "TEARDOWN",
    "MCPAdapter", "MCPClient", "StdioTransport",
    "MockCloudTransport", "VirtualClock",
    "CloudProfile", "REDU", "AWS", "GCP", "AZURE", "PROFILES", "profiles",
]
