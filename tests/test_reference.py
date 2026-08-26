"""Tests for the Part 3 reference-optimal model: the floor cost-to-go, the exact excess-over-floor
decomposition (telescoping advantages and the floor-twin selection/execution split), the two-ratio
bracket, and the refutable gold."""
import math
import unittest

from acspeed.reference import (
    Edge, ReferenceGraph, decompose, execution_excess_only, gold_is_refuted, two_ratios,
)


def graph():
    # start -> A -> goal is the short floor path (3 + 6 = 9); start -> goal direct is longer (12).
    return ReferenceGraph(
        edges=[
            Edge("start", "A", "provision", floor=3.0),
            Edge("A", "goal", "materialize", floor=6.0),
            Edge("start", "goal", "provision_and_serve_direct", floor=12.0),
        ],
        start="start",
        goal="goal",
    )


class TestGraphAndFloor(unittest.TestCase):
    def test_cost_to_go(self):
        vf = graph().cost_to_go()
        self.assertAlmostEqual(vf["goal"], 0.0)
        self.assertAlmostEqual(vf["A"], 6.0)
        self.assertAlmostEqual(vf["start"], 9.0)  # min(3+6, 12)

    def test_floor_makespan_is_shortest_path(self):
        self.assertAlmostEqual(graph().floor_makespan(), 9.0)

    def test_unreachable_goal_raises(self):
        g = ReferenceGraph([Edge("start", "A", "x", floor=1.0)], start="start", goal="goal")
        with self.assertRaises(ValueError):
            g.floor_makespan()


class TestEdgeGuards(unittest.TestCase):
    def test_actual_below_floor_raises(self):
        with self.assertRaises(ValueError):
            Edge("start", "A", "x", floor=5.0, actual=3.0)

    def test_negative_floor_raises(self):
        with self.assertRaises(ValueError):
            Edge("start", "A", "x", floor=-1.0)


class TestDecomposition(unittest.TestCase):
    def edges(self, spec):
        # spec: list of (label, actual) picking edges by label from the graph
        by_label = {e.label: e for e in graph().edges}
        return [Edge(by_label[l].src, by_label[l].dst, l, by_label[l].floor, actual=a) for l, a in spec]

    def test_optimal_at_floor_is_zero_excess(self):
        d = decompose(graph(), self.edges([("provision", 3.0), ("materialize", 6.0)]))
        self.assertAlmostEqual(d.m_actual, 9.0)
        self.assertAlmostEqual(d.floor, 9.0)
        self.assertAlmostEqual(d.excess_over_floor, 0.0)
        self.assertAlmostEqual(d.selection_excess, 0.0)
        self.assertAlmostEqual(d.execution_excess, 0.0)
        self.assertTrue(all(abs(a) < 1e-9 for a in d.advantages))
        self.assertTrue(d.identity_holds)

    def test_optimal_path_slow_execution(self):
        d = decompose(graph(), self.edges([("provision", 5.0), ("materialize", 6.0)]))
        self.assertAlmostEqual(d.excess_over_floor, 2.0)
        self.assertAlmostEqual(d.execution_excess, 2.0)   # ran provision 2s over its floor
        self.assertAlmostEqual(d.selection_excess, 0.0)   # right path
        self.assertAlmostEqual(d.advantages[0], 2.0)      # 5 + V_F[A]=6 - V_F[start]=9
        self.assertAlmostEqual(d.advantages[1], 0.0)
        self.assertTrue(d.identity_holds)

    def test_wrong_path_at_floor_is_pure_selection(self):
        d = decompose(graph(), self.edges([("provision_and_serve_direct", 12.0)]))
        self.assertAlmostEqual(d.excess_over_floor, 3.0)
        self.assertAlmostEqual(d.execution_excess, 0.0)   # ran at floor
        self.assertAlmostEqual(d.selection_excess, 3.0)   # 12 chosen vs 9 optimal floor
        self.assertAlmostEqual(d.advantages[0], 3.0)      # the wrong branch pinned to one decision
        self.assertTrue(d.identity_holds)

    def test_wrong_path_and_slow_sums_exactly(self):
        d = decompose(graph(), self.edges([("provision_and_serve_direct", 14.0)]))
        self.assertAlmostEqual(d.excess_over_floor, 5.0)
        self.assertAlmostEqual(d.execution_excess, 2.0)
        self.assertAlmostEqual(d.selection_excess, 3.0)
        self.assertAlmostEqual(sum(d.advantages), 5.0)
        self.assertTrue(d.identity_holds)

    def test_non_completing_trajectory_raises(self):
        with self.assertRaises(ValueError):
            decompose(graph(), self.edges([("provision", 3.0)]))  # stops at A, not goal

    def test_non_contiguous_trajectory_raises(self):
        by_label = {e.label: e for e in graph().edges}
        traj = [
            Edge("start", "A", "provision", by_label["provision"].floor, actual=3.0),
            Edge("start", "goal", "provision_and_serve_direct", by_label["provision_and_serve_direct"].floor, actual=12.0),
        ]
        with self.assertRaises(ValueError):
            decompose(graph(), traj)


class TestOffSuiteAndRatios(unittest.TestCase):
    def test_execution_excess_only(self):
        traj = [
            Edge("start", "A", "provision", 3.0, actual=5.0),
            Edge("A", "goal", "materialize", 6.0, actual=6.0),
        ]
        self.assertAlmostEqual(execution_excess_only(traj), 2.0)

    def test_two_ratios_and_bracket(self):
        r = two_ratios(m_actual=11.0, floor=9.0, best_achieved=10.0)
        self.assertAlmostEqual(r["floor_ratio"], 11.0 / 9.0)
        self.assertAlmostEqual(r["best_ratio"], 1.1)
        self.assertAlmostEqual(r["bracket_low"], 9.0)
        self.assertAlmostEqual(r["bracket_high"], 10.0)

    def test_best_below_floor_raises(self):
        with self.assertRaises(ValueError):
            two_ratios(m_actual=11.0, floor=9.0, best_achieved=8.0)

    def test_gold_refutation(self):
        self.assertIsNone(gold_is_refuted(9.0, [10.0, 11.0]))     # gold stands
        self.assertAlmostEqual(gold_is_refuted(10.0, [9.0, 11.0]), 9.0)  # beaten -> update to 9


if __name__ == "__main__":
    unittest.main()
