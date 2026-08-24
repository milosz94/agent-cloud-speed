import unittest

from acspeed import capability as cap
from acspeed.types import CVector

REF = CVector(40000, 16, 10000, 10)


class TestCapability(unittest.TestCase):
    def test_ratios_identity(self):
        r = cap.ratios(REF, REF)
        for a in cap.AXES:
            self.assertAlmostEqual(r[a], 1.0)

    def test_dci_identity(self):
        self.assertAlmostEqual(cap.dci(REF, REF), 1.0)

    def test_dci_single_axis_doubled(self):
        m = CVector(80000, 16, 10000, 10)  # compute x2, others equal
        self.assertAlmostEqual(cap.dci(m, REF), 2 ** 0.25, places=6)

    def test_dci_weighted(self):
        m = CVector(80000, 16, 10000, 10)
        w = {"compute": 1.0, "memory": 0.0, "disk": 0.0, "network": 0.0}
        self.assertAlmostEqual(cap.dci(m, REF, w), 2.0)

    def test_weights_must_sum_to_one(self):
        with self.assertRaises(ValueError):
            cap.dci(REF, REF, {"compute": 0.5, "memory": 0.1, "disk": 0.1, "network": 0.1})

    def test_reference_axis_positive(self):
        with self.assertRaises(ValueError):
            cap.ratios(REF, CVector(0, 16, 10000, 10))

    def test_dominant_axis(self):
        self.assertEqual(cap.dominant_axis({"compute": 0.9, "disk": 0.4}), "compute")

    def test_normalize_phase(self):
        self.assertAlmostEqual(cap.normalize_phase(10.0, 2.0), 5.0)


if __name__ == "__main__":
    unittest.main()
