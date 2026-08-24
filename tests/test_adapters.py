import unittest
from dataclasses import replace

from acspeed.adapters import profiles
from acspeed.adapters.base import OperationSpec
from acspeed.adapters.mcp_adapter import MCPAdapter
from acspeed.adapters.mcp_client import MCPClient
from acspeed.adapters.mock import MockCloudTransport, VirtualClock
from acspeed.agenttime import decompose


def build(readiness, boot=9.0, fail=0, interval=3.0):
    clock = VirtualClock()
    prof = replace(profiles.REDU, readiness=readiness, poll_interval=interval)
    mock = MockCloudTransport(clock, prof.tool_map, boot=boot, fail_provision_times=fail)
    client = MCPClient(mock)
    return MCPAdapter(client, prof, clock=clock.now, sleep=clock.sleep)


class TestAdapters(unittest.TestCase):
    def test_mcp_client_handshake(self):
        clock = VirtualClock()
        mock = MockCloudTransport(clock, profiles.REDU.tool_map)
        c = MCPClient(mock)
        self.assertIn("serverInfo", c.initialize())
        self.assertGreaterEqual(len(c.list_tools()), 1)

    def test_blocking_wait_is_platform(self):
        d = decompose(build("blocking", boot=9.0).run(OperationSpec()))
        self.assertAlmostEqual(d["critical_agent_time"], 0.0)
        self.assertGreater(d["critical_platform_time"], 8.0)  # includes the boot
        self.assertAlmostEqual(d["critical_platform_time"], d["makespan"])

    def test_poll_moves_wait_to_agent(self):
        d = decompose(build("poll", boot=9.0, interval=3.0).run(OperationSpec()))
        self.assertGreater(d["critical_agent_time"], 8.0)          # the agent polls ~ boot
        self.assertLess(d["critical_platform_time"], d["critical_agent_time"])
        self.assertGreater(d["raw_components"]["wait"], 8.0)

    def test_interface_effect(self):
        b = decompose(build("blocking", boot=9.0).run(OperationSpec()))
        p = decompose(build("poll", boot=9.0, interval=3.0).run(OperationSpec()))
        # same cloud + boot: poll charges the wait to the agent, blocking to the platform
        self.assertGreater(p["critical_agent_time"], b["critical_agent_time"])
        self.assertGreater(b["critical_platform_time"], p["critical_platform_time"])

    def test_provision_failure_is_platform_rework(self):
        spans = build("blocking", boot=5.0, fail=1).run(OperationSpec())
        self.assertIn("rework", [s.kind for s in spans])           # cloud-fault retry
        self.assertIn("provision", [s.id for s in spans])          # still completes
        # the rework span is charged to the platform, not the agent
        rework = [s for s in spans if s.kind == "rework"][0]
        self.assertEqual(rework.owner, "platform")


if __name__ == "__main__":
    unittest.main()
