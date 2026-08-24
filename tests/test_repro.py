import unittest

from acspeed import repro as r
from acspeed.types import Estimate


class TestRepro(unittest.TestCase):
    def test_geomean(self):
        self.assertAlmostEqual(r.geomean([1, 4]), 2.0)
        self.assertAlmostEqual(r.geomean([2, 8]), 4.0)

    def test_geomean_needs_positive(self):
        with self.assertRaises(ValueError):
            r.geomean([1, -1])

    def test_mean_ci_constant(self):
        e = r.mean_ci([10.0, 10.0, 10.0])
        self.assertAlmostEqual(e.value, 10.0)
        self.assertAlmostEqual(e.half_width, 0.0)

    def test_mean_ci_center(self):
        e = r.mean_ci([1, 2, 3, 4, 5])
        self.assertAlmostEqual(e.value, 3.0)
        self.assertLess(e.lo, 3.0)
        self.assertGreater(e.hi, 3.0)

    def test_bootstrap_constant(self):
        e = r.bootstrap_ci([5.0, 5.0, 5.0, 5.0])
        self.assertAlmostEqual(e.value, 5.0)
        self.assertAlmostEqual(e.lo, 5.0)
        self.assertAlmostEqual(e.hi, 5.0)

    def test_bootstrap_deterministic(self):
        s = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        self.assertEqual(r.bootstrap_ci(s, seed=42).lo, r.bootstrap_ci(s, seed=42).lo)

    def test_confirm_constant(self):
        e = r.confirm(lambda: 4.0, target_rel_halfwidth=0.01, min_n=3)
        self.assertAlmostEqual(e.value, 4.0)
        self.assertGreaterEqual(e.n, 3)

    def test_different_by_non_overlap(self):
        a = Estimate(1.0, 0.0, 2.0)
        b = Estimate(5.0, 4.0, 6.0)
        c = Estimate(1.5, 1.0, 2.0)
        self.assertTrue(r.different(a, b))   # disjoint intervals
        self.assertFalse(r.different(a, c))  # overlapping intervals


if __name__ == "__main__":
    unittest.main()
