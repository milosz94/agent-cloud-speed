import unittest

from acspeed import agenttime as at
from acspeed.types import Span


def deploy_trace():
    return [
        Span("s1", 2, "agent", (), "inference"),
        Span("s2", 10, "platform", ("s1",), "provision"),
        Span("s3", 4, "agent", ("s1",), "inference"),
        Span("s4", 6, "platform", ("s2", "s3"), "boot"),
        Span("s5", 3, "agent", ("s4",), "orchestration"),
        Span("s6", 2, "agent", ("s5",), "rework"),
    ]


class TestAgentTime(unittest.TestCase):
    def test_decompose_totals(self):
        r = at.decompose(deploy_trace())
        self.assertAlmostEqual(r["makespan"], 23.0)
        self.assertAlmostEqual(r["raw_agent_time"], 11.0)
        self.assertAlmostEqual(r["critical_agent_time"], 7.0)
        self.assertAlmostEqual(r["overlap_agent_time"], 4.0)
        self.assertAlmostEqual(r["critical_platform_time"], 16.0)
        # critical agent + critical platform == wall-clock (Eq. 1)
        self.assertAlmostEqual(
            r["critical_agent_time"] + r["critical_platform_time"], r["makespan"]
        )

    def test_components(self):
        r = at.decompose(deploy_trace())
        self.assertAlmostEqual(r["raw_components"]["inference"], 6.0)       # s1 + s3
        self.assertAlmostEqual(r["raw_components"]["orchestration"], 3.0)   # s5
        self.assertAlmostEqual(r["raw_components"]["rework"], 2.0)          # s6
        self.assertAlmostEqual(r["raw_components"]["wait"], 0.0)
        self.assertAlmostEqual(r["critical_components"]["inference"], 2.0)  # only s1 on path
        self.assertAlmostEqual(r["critical_components"]["orchestration"], 3.0)

    def test_inference_seconds(self):
        # TTFT prices the first token; TPOT prices the other 99, not all 100.
        self.assertAlmostEqual(at.inference_seconds(0.5, 0.01, 100), 1.49)

    def test_inference_seconds_first_token_not_double_charged(self):
        # A one-token generation costs exactly TTFT: no inter-token gaps exist.
        self.assertAlmostEqual(at.inference_seconds(0.5, 0.01, 1), 0.5)
        # And no tokens means no generation latency at all.
        self.assertAlmostEqual(at.inference_seconds(0.5, 0.01, 0), 0.0)

    def test_inference_seconds_negative(self):
        with self.assertRaises(ValueError):
            at.inference_seconds(-1, 0.01, 100)


if __name__ == "__main__":
    unittest.main()
