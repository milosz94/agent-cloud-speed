"""Tests for the Part 4 weighting/aggregation model: the matched-block per-task verdict, the total-time
headline and geometric-mean companion (and their agree-or-report-split rule), the drop-any-task
sensitivity, and the (wall-clock, resource-cost) Pareto-Koopmans cost-performance frontier."""
import math
import unittest

from acspeed.weighting import (
    Run, classify_run, dominates, geomean_ratio, is_tradeoff, leave_one_out,
    more_efficient, pareto_frontier, suite_total, suite_verdict,
)


class TestMatchedBlock(unittest.TestCase):
    def test_distinguishable_faster_cloud_wins(self):
        # Tight, well-separated distributions -> non-overlapping CIs -> a verdict.
        self.assertEqual(more_efficient([1.0] * 5, [5.0] * 5), "A")
        self.assertEqual(more_efficient([5.0] * 5, [1.0] * 5), "B")

    def test_overlapping_is_indistinguishable(self):
        # Identical distributions overlap -> no verdict (never decide on a single pair).
        self.assertEqual(more_efficient([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]), "indistinguishable")

    def test_empty_samples_raise(self):
        with self.assertRaises(ValueError):
            more_efficient([], [1.0])


class TestSuiteTotal(unittest.TestCase):
    def test_sum_over_tasks(self):
        self.assertAlmostEqual(suite_total({"t1": 3.0, "t2": 8.0}), 11.0)
        self.assertAlmostEqual(suite_total([3.0, 8.0]), 11.0)

    def test_empty_and_negative_raise(self):
        with self.assertRaises(ValueError):
            suite_total({})
        with self.assertRaises(ValueError):
            suite_total({"t": -1.0})


class TestGeomeanCompanion(unittest.TestCase):
    def test_known_value(self):
        gm = geomean_ratio({"t1": 8.0, "t2": 8.0}, {"t1": 10.0, "t2": 10.0})
        self.assertAlmostEqual(gm, 0.8)

    def test_mismatched_tasks_raise(self):
        with self.assertRaises(ValueError):
            geomean_ratio({"t1": 1.0}, {"t2": 1.0})


class TestSuiteVerdict(unittest.TestCase):
    def test_agreement_gives_a_verdict(self):
        ref = {"t1": 10.0, "t2": 10.0}
        a = {"t1": 8.0, "t2": 8.0}
        b = {"t1": 10.0, "t2": 10.0}
        v = suite_verdict(a, b, ref)
        self.assertTrue(v["agree"])
        self.assertEqual(v["verdict"], "A")

    def test_disagreement_is_unresolved(self):
        # A wins on total (better on the big task); B wins on the equal-vote geomean (better on the small).
        ref = {"big": 100.0, "small": 1.0}
        a = {"big": 90.0, "small": 2.0}   # total 92; ratios 0.9, 2.0
        b = {"big": 100.0, "small": 0.5}  # total 100.5; ratios 1.0, 0.5
        v = suite_verdict(a, b, ref)
        self.assertEqual(v["total_winner"], "A")
        self.assertEqual(v["geomean_winner"], "B")
        self.assertFalse(v["agree"])
        self.assertEqual(v["verdict"], "unresolved")


class TestLeaveOneOut(unittest.TestCase):
    def test_robust_ranking_survives(self):
        a = {"t1": 1.0, "t2": 1.0, "t3": 1.0}
        b = {"t1": 2.0, "t2": 2.0, "t3": 2.0}
        r = leave_one_out(a, b)
        self.assertEqual(r["full_verdict"], "A")
        self.assertTrue(r["robust"])
        self.assertEqual(r["flips_on"], [])

    def test_ranking_that_flips_on_one_task(self):
        # A wins overall only because 'small' is where B is far worse; drop it and B wins.
        a = {"big": 10.0, "small": 1.0}   # total 11
        b = {"big": 1.0, "small": 20.0}   # total 21
        r = leave_one_out(a, b)
        self.assertEqual(r["full_verdict"], "A")
        self.assertFalse(r["robust"])
        self.assertEqual(r["flips_on"], ["small"])

    def test_needs_two_tasks(self):
        with self.assertRaises(ValueError):
            leave_one_out({"t": 1.0}, {"t": 2.0})


class TestCostPerformanceFrontier(unittest.TestCase):
    def test_faster_and_cheaper_dominates(self):
        a = Run("a", time=1.0, cost=5.0)
        b = Run("b", time=2.0, cost=10.0)
        self.assertTrue(dominates(a, b))
        self.assertFalse(dominates(b, a))
        self.assertFalse(is_tradeoff(a, b))

    def test_time_for_money_is_a_tradeoff(self):
        # buying speed: faster but pricier vs slower but cheaper -> mutually non-dominated.
        fast = Run("fast", time=1.0, cost=10.0)
        slow = Run("slow", time=5.0, cost=2.0)
        self.assertTrue(is_tradeoff(fast, slow))
        frontier = {r.label for r in pareto_frontier([fast, slow])}
        self.assertEqual(frontier, {"fast", "slow"})

    def test_cost_slack_is_dominated(self):
        # same time, more cost: bought no speed -> a positive cost slack -> inefficient.
        base = Run("base", time=3.0, cost=3.0)
        slack = Run("slack", time=3.0, cost=5.0)
        self.assertTrue(dominates(base, slack))
        c = classify_run(slack, [base, slack])
        self.assertFalse(c["efficient"])
        self.assertEqual(c["reason"], "cost_slack")
        self.assertIn("base", c["dominated_by"])

    def test_slower_is_dominated(self):
        base = Run("base", time=3.0, cost=3.0)
        slow = Run("slow", time=5.0, cost=3.0)
        self.assertTrue(dominates(base, slow))
        c = classify_run(slow, [base, slow])
        self.assertFalse(c["efficient"])
        self.assertEqual(c["reason"], "slower")

    def test_frontier_keeps_only_efficient(self):
        winner = Run("winner", time=1.0, cost=1.0)      # dominates everything below
        tradeoff = Run("tradeoff", time=0.5, cost=4.0)  # faster than winner, pricier -> non-dominated
        slack = Run("slack", time=1.0, cost=3.0)        # winner dominates (same time, more cost)
        slow = Run("slow", time=9.0, cost=9.0)          # winner dominates
        frontier = {r.label for r in pareto_frontier([winner, tradeoff, slack, slow])}
        self.assertEqual(frontier, {"winner", "tradeoff"})
        self.assertTrue(classify_run(winner, [winner, tradeoff, slack, slow])["efficient"])
        self.assertFalse(classify_run(slack, [winner, tradeoff, slack, slow])["efficient"])

    def test_negative_input_rejected(self):
        with self.assertRaises(ValueError):
            Run("bad", time=-1.0, cost=1.0)


if __name__ == "__main__":
    unittest.main()
