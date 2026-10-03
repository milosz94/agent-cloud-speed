"""tools/elapsed_span.py: the parts of the elapsed span that M leaves out sum to the difference (Part 5, section 4).

A synthetic record pins the decomposition; the record case pins the published figures when the tree is present.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))


def _win(start, end, makespan=None):
    w = {"started_at": start, "verified_at": end, "wall_s": end - start}
    if makespan is not None:
        w["split"] = {"makespan_s": makespan}
    return w


class Decomposition(unittest.TestCase):
    def test_parts_sum_to_elapsed_minus_m(self):
        import elapsed_span as es
        rec = {"split": {"makespan_s": 100.0},                       # deploy leg: t0 .. t0+100 (t1)
               "tier_run": {"operations": [_win(1130, 1131), _win(1131, 1171, 30.0)],
                            "durability": {"cycles": [_win(1171, 1201, 25.0), _win(1211, 1251, 32.0)]}}}
        from unittest import mock
        with mock.patch.object(es.pt, "task_M", return_value=100.0 + 30.0 + 25.0 + 32.0):
            m, el, parts = es.run_parts(rec, 1000.0)
        self.assertAlmostEqual(el, 251.0)
        self.assertAlmostEqual(parts["t1 -> first window"], 30.0)  # 1100 -> 1130
        self.assertAlmostEqual(parts["between cycles"], 10.0)      # 1201 -> 1211
        self.assertAlmostEqual(parts["window edges"], 10.0 + 5.0 + 8.0)
        self.assertAlmostEqual(parts["unsplit windows"], 1.0)
        self.assertAlmostEqual(sum(parts.values()), el - m)

    def test_published_medium(self):
        import elapsed_span as es
        try:
            runs = es.medium_runs()
        except (OSError, KeyError, TypeError) as e:          # published tree absent
            self.skipTest("published records not available: %s" % e)
        every = [r for v in runs.values() for r in v]
        self.assertEqual(len(every), 62)
        excl = sum(el - m for m, el, _p, _a in every)
        self.assertAlmostEqual(excl, 31788, delta=1)
        tot = {k: sum(p[k] for _m, _el, p, _a in every) for k in es.PARTS}
        self.assertAlmostEqual(sum(tot.values()), excl, delta=1)
        self.assertAlmostEqual(tot["t1 -> first window"], 21716, delta=1)
        self.assertAlmostEqual(tot["between cycles"], 3288, delta=1)
        self.assertAlmostEqual(tot["window edges"], 6742, delta=1)
        self.assertAlmostEqual(sum(a for *_x, a in every), 17222, delta=1)


if __name__ == "__main__":
    unittest.main()
