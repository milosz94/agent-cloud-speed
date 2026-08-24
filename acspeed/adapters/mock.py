"""A virtual-clock mock MCP server transport for offline, deterministic adapter
tests. It simulates per-tool latency and a boot that becomes ready after a set
delay, so adapter timing and span emission are tested without a real cloud and
without real sleeps.
"""
from __future__ import annotations

from typing import Dict, Optional


class VirtualClock:
    """A deterministic monotonic clock advanced explicitly by the mock/adapter."""

    def __init__(self, t: float = 0.0):
        self._t = float(t)

    def now(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        if dt < 0:
            raise ValueError("cannot advance the clock backwards")
        self._t += dt

    def sleep(self, dt: float) -> None:
        self.advance(dt)


class MockCloudTransport:
    """Mock MCP server. ``tool_map`` (canonical -> tool name) lets it recognise
    which tool is which. The provision tool starts a boot of ``boot`` seconds;
    ``status`` reports ready once the boot has elapsed; ``wait_ready`` blocks
    server-side until ready. ``fail_provision_times`` makes the first N provision
    calls fail (to exercise cloud-fault rework accounting).
    """

    def __init__(self, clock: VirtualClock, tool_map: Dict[str, str], boot: float = 9.0,
                 latencies: Optional[Dict[str, float]] = None, default_lat: float = 0.05,
                 fail_provision_times: int = 0):
        self.clock = clock
        self.canon = {v: k for k, v in tool_map.items()}  # tool name -> canonical
        self.boot = boot
        self.lat = latencies or {}
        self.default_lat = default_lat
        self.ready_at: Optional[float] = None
        self.fail_provision_times = fail_provision_times
        self._prov_calls = 0

    def request(self, method: str, params: dict = None) -> dict:
        params = params or {}
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "serverInfo": {"name": "mock-cloud"}}
        if method == "tools/list":
            return {"tools": [{"name": n} for n in sorted(self.canon)]}
        if method == "tools/call":
            name = params["name"]
            canonical = self.canon.get(name, name)
            lat = self.lat.get(name, self.default_lat)

            if canonical == "provision":
                self.clock.advance(lat)
                self._prov_calls += 1
                if self._prov_calls <= self.fail_provision_times:
                    raise RuntimeError("mock provision failure")
                self.ready_at = self.clock.now() + self.boot
                return {"content": [{"type": "text", "text": "created"}], "instance": "i-mock"}

            if canonical == "wait_ready":
                target = self.ready_at if self.ready_at is not None else self.clock.now()
                self.clock.advance(max(0.0, target - self.clock.now()))
                return {"ready": True, "content": [{"type": "text", "text": "ready"}]}

            if canonical == "status":
                self.clock.advance(lat)
                ready = self.ready_at is not None and self.clock.now() >= self.ready_at
                return {"ready": ready}

            if canonical == "teardown":
                self.clock.advance(lat)
                return {"content": [{"type": "text", "text": "deleted"}]}

            self.clock.advance(lat)
            return {"content": []}
        return {}

    def notify(self, method: str, params: dict = None) -> None:
        pass
