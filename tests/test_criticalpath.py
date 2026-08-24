import unittest

from acspeed import criticalpath as cp
from acspeed.types import Span


def deploy_trace():
    """A deploy where the agent's config-prep (s3) overlaps the provision (s2)."""
    return [
        Span("s1", 2, "agent", (), "inference"),
        Span("s2", 10, "platform", ("s1",), "provision"),
        Span("s3", 4, "agent", ("s1",), "inference"),
        Span("s4", 6, "platform", ("s2", "s3"), "boot"),
        Span("s5", 3, "agent", ("s4",), "orchestration"),
        Span("s6", 2, "agent", ("s5",), "rework"),
    ]


class TestCriticalPath(unittest.TestCase):
    def test_makespan(self):
        self.assertAlmostEqual(cp.schedule(deploy_trace())["makespan"], 23.0)

    def test_critical_chain(self):
        ids = [s.id for s in cp.critical_path(deploy_trace())]
        self.assertEqual(ids, ["s1", "s2", "s4", "s5", "s6"])

    def test_slack(self):
        sched = cp.schedule(deploy_trace())
        self.assertAlmostEqual(sched["slack"]["s3"], 6.0)  # off-path, has float
        self.assertAlmostEqual(sched["slack"]["s1"], 0.0)  # on the path

    def test_is_critical(self):
        crit = cp.is_critical(deploy_trace())
        self.assertTrue(crit["s2"])
        self.assertFalse(crit["s3"])

    def test_owner_split_partitions_makespan(self):
        sp = cp.owner_split(deploy_trace())
        self.assertAlmostEqual(sp["makespan"], 23.0)
        self.assertAlmostEqual(sp["owners"]["agent"]["critical"], 7.0)
        self.assertAlmostEqual(sp["owners"]["platform"]["critical"], 16.0)
        self.assertAlmostEqual(sp["owners"]["agent"]["raw"], 11.0)
        self.assertAlmostEqual(sp["owners"]["agent"]["overlap"], 4.0)
        total_crit = sum(o["critical"] for o in sp["owners"].values())
        self.assertAlmostEqual(total_crit, sp["makespan"])

    def test_cycle_detected(self):
        spans = [Span("a", 1, "agent", ("b",)), Span("b", 1, "agent", ("a",))]
        with self.assertRaises(ValueError):
            cp.schedule(spans)

    def test_unknown_dep(self):
        with self.assertRaises(ValueError):
            cp.schedule([Span("a", 1, "agent", ("ghost",))])

    def test_duplicate_id(self):
        with self.assertRaises(ValueError):
            cp.schedule([Span("a", 1, "agent"), Span("a", 2, "platform")])

    def test_single_span(self):
        sp = cp.owner_split([Span("only", 5, "platform", (), "provision")])
        self.assertAlmostEqual(sp["makespan"], 5.0)
        self.assertAlmostEqual(sp["owners"]["platform"]["critical"], 5.0)

    def test_empty(self):
        self.assertEqual(cp.schedule([])["makespan"], 0.0)
        self.assertEqual(cp.critical_path([]), [])


if __name__ == "__main__":
    unittest.main()
