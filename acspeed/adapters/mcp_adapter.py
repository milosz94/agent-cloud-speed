"""MCPAdapter: drive a canonical cloud operation through an MCP server and emit a
timed, owner-labelled trace. The same code serves every cloud; only the profile
differs.

It surfaces the interface effect directly: with a blocking readiness tool the
wait is a single server-side call attributed to the PLATFORM (the agent is free
to overlap), while a poll loop forces the agent to occupy the wait, attributing
it to the AGENT. A cloud-fault provision retry is recorded as a platform rework
span, per the paper's accounting rule (a retry forced by the cloud is charged to
the platform, not the agent).
"""
from __future__ import annotations

import time
from typing import Callable, List

from ..types import AGENT, PLATFORM, Span
from .base import PROVISION, STATUS, WAIT_READY, CloudAdapter, OperationSpec
from .mcp_client import MCPClient


class MCPAdapter(CloudAdapter):
    def __init__(self, client: MCPClient, profile,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 max_provision_retries: int = 3, max_polls: int = 100000):
        self.client = client
        self.profile = profile
        self.clock = clock
        self.sleep = sleep
        self.max_provision_retries = max_provision_retries
        self.max_polls = max_polls
        self.name = profile.name

    def run(self, spec: OperationSpec) -> List[Span]:
        spans: List[Span] = []
        prev = None

        # provision, retrying cloud faults (each failed attempt = platform rework)
        attempt = 0
        while True:
            t0 = self.clock()
            try:
                self.client.call_tool(self.profile.tool(PROVISION), spec.params)
                spans.append(Span("provision", self.clock() - t0, PLATFORM,
                                  () if prev is None else (prev,), "provision"))
                prev = "provision"
                break
            except Exception:
                fid = f"provision_fail_{attempt}"
                spans.append(Span(fid, self.clock() - t0, PLATFORM,
                                  () if prev is None else (prev,), "rework"))
                prev = fid
                attempt += 1
                if attempt > self.max_provision_retries:
                    raise

        # readiness
        if self.profile.readiness == "blocking":
            t0 = self.clock()
            self.client.call_tool(self.profile.tool(WAIT_READY), {})
            spans.append(Span("wait_ready", self.clock() - t0, PLATFORM, (prev,), "boot"))
        else:
            i = 0
            while i < self.max_polls:
                t0 = self.clock()
                sres = self.client.call_tool(self.profile.tool(STATUS), {})
                ready = bool(sres.get("ready", False))
                if not ready:
                    self.sleep(self.profile.poll_interval)
                spans.append(Span(f"poll_{i}", self.clock() - t0, AGENT, (prev,), "wait"))
                prev = f"poll_{i}"
                i += 1
                if ready:
                    break

        return spans
