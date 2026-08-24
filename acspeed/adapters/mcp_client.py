"""A minimal MCP (Model Context Protocol) client over a pluggable transport.

MCP is JSON-RPC 2.0. This client implements the subset the harness needs:
``initialize``, ``tools/list`` and ``tools/call``. The wire framing lives in the
transport, so a mock transport can be injected for offline tests and a real
stdio transport talks to an MCP server subprocess.

Note: this is a pragmatic subset validated against the mock. Confirm the exact
handshake against the target server and the current MCP spec when wiring a real
cloud's MCP server.
"""
from __future__ import annotations

import json
import subprocess
from typing import List


class MCPClient:
    """Speaks MCP over a transport exposing ``request(method, params)`` and
    ``notify(method, params)``."""

    def __init__(self, transport):
        self.t = transport
        self.initialized = False

    def initialize(self) -> dict:
        res = self.t.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "acspeed", "version": "0.1.0"},
        })
        self.t.notify("notifications/initialized", {})
        self.initialized = True
        return res

    def list_tools(self) -> List[dict]:
        return self.t.request("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        if not self.initialized:
            self.initialize()
        return self.t.request("tools/call", {"name": name, "arguments": arguments})


class StdioTransport:
    """Newline-delimited JSON-RPC 2.0 over an MCP server subprocess (stdio).

    Requires the server command on PATH; not exercised by the offline tests
    (the tested logic lives in the mock transport).
    """

    def __init__(self, command: List[str]):
        self.proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1
        )
        self._id = 0

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        msg = json.loads(line)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg.get("result", {})

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        try:
            self.proc.terminate()
        except Exception:
            pass
