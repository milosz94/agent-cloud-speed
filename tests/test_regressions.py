"""Regression tests for the five findings from the adversarial review."""
import math
import unittest

from acspeed import agenttime as at
from acspeed import criticalpath as cp
from acspeed import discriminator as d
from acspeed import repro as r
from acspeed import traceio
from acspeed.types import Span


class TestCriticalPathLargeMagnitude(unittest.TestCase):
    def test_partition_invariant_holds_at_large_magnitude(self):
        # cumulative wall-clock >> 8e6, where the old subtraction+epsilon broke
        spans = [
            Span("root", 8436660.5213498, "platform", ()),
            Span("sink", 278.62235041242093, "agent", ("root",)),
        ]
        chain = [s.id for s in cp.critical_path(spans)]
        self.assertEqual(chain, ["root", "sink"])  # both on the path
        sp = cp.owner_split(spans)
        total_crit = sum(o["critical"] for o in sp["owners"].values())
        self.assertAlmostEqual(total_crit, sp["makespan"], places=3)

    def test_long_serial_chain_fully_critical(self):
        spans = [Span("s0", 1e4, "agent", ())]
        for i in range(1, 3000):
            spans.append(Span(f"s{i}", 1e4, "agent" if i % 2 else "platform", (f"s{i-1}",)))
        chain = cp.critical_path(spans)
        self.assertEqual(len(chain), 3000)  # every span is on the serial path
        sp = cp.owner_split(spans)
        total_crit = sum(o["critical"] for o in sp["owners"].values())
        self.assertAlmostEqual(total_crit, sp["makespan"], places=2)


class TestPlaneSharesValidity(unittest.TestCase):
    def test_negative_floor_is_rejected(self):
        fit = d.fit_fixed_variable([1.0, 2.0, 4.0, 8.0], [18.0, 11.0, 4.5, 1.0])
        self.assertLess(fit["t_fixed"], 0.0)          # OLS gives a negative intercept
        self.assertFalse(d.fit_is_valid(fit))
        with self.assertRaises(ValueError):
            d.plane_shares(fit, 8.0)

    def test_valid_fit_shares_in_range(self):
        fit = {"t_fixed": 2.0, "W": 20.0, "r_squared": 1.0}
        self.assertTrue(d.fit_is_valid(fit))
        s = d.plane_shares(fit, 2.0)
        self.assertTrue(0.0 <= s["control_plane"] <= 1.0)
        self.assertTrue(0.0 <= s["data_plane"] <= 1.0)


class TestReproSingleSample(unittest.TestCase):
    def test_mean_ci_n1_is_unbounded(self):
        e = r.mean_ci([10.0])
        self.assertEqual(e.lo, -math.inf)
        self.assertEqual(e.hi, math.inf)

    def test_two_single_samples_not_declared_different(self):
        self.assertFalse(r.different(r.mean_ci([10.0]), r.mean_ci([10.5])))

    def test_confirm_does_not_converge_on_one_sample(self):
        e = r.confirm(lambda: 4.0, target_rel_halfwidth=0.01, min_n=1)
        self.assertGreaterEqual(e.n, 2)  # cannot claim convergence from n=1

    def test_bootstrap_n1_is_unbounded(self):
        e = r.bootstrap_ci([7.0])
        self.assertEqual(e.lo, -math.inf)
        self.assertEqual(e.hi, math.inf)


class TestTraceIoNullDeps(unittest.TestCase):
    def test_null_deps_loads_as_empty(self):
        spans = traceio.spans_from_dicts([
            {"id": "s1", "duration": 2, "owner": "agent", "deps": None, "kind": "inference"},
        ])
        self.assertEqual(spans[0].deps, ())


class TestAgentTimeComponentsPartition(unittest.TestCase):
    def test_unknown_and_empty_kinds_go_to_other(self):
        spans = [
            Span("s1", 3, "agent"),                 # kind defaults to ""
            Span("s2", 4, "agent", ("s1",), "custom"),
            Span("s3", 5, "platform", ("s2",)),
        ]
        res = at.decompose(spans)
        self.assertAlmostEqual(res["raw_agent_time"], 7.0)
        self.assertAlmostEqual(sum(res["raw_components"].values()), res["raw_agent_time"])
        self.assertAlmostEqual(res["raw_components"]["other"], 7.0)


if __name__ == "__main__":
    unittest.main()
