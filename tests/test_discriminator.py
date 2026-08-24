import unittest

from acspeed import discriminator as d


class TestDiscriminator(unittest.TestCase):
    def test_fit_recovers_floor_and_work(self):
        # T = 2 + 20/rate  ->  t_fixed = 2 (control-plane floor), W = 20 (data-plane)
        rates = [1.0, 2.0, 4.0, 8.0]
        times = [2.0 + 20.0 / r for r in rates]
        fit = d.fit_fixed_variable(rates, times)
        self.assertAlmostEqual(fit["t_fixed"], 2.0, places=6)
        self.assertAlmostEqual(fit["W"], 20.0, places=6)
        self.assertAlmostEqual(fit["r_squared"], 1.0, places=6)

    def test_plane_shares(self):
        fit = {"t_fixed": 2.0, "W": 20.0, "r_squared": 1.0}
        s = d.plane_shares(fit, 2.0)  # data = 10, total = 12
        self.assertAlmostEqual(s["control_plane"], 2 / 12)
        self.assertAlmostEqual(s["data_plane"], 10 / 12)
        self.assertAlmostEqual(s["predicted"], 12.0)

    def test_karp_flatt(self):
        es = d.karp_flatt([1.8, 3.2], [2, 4])
        self.assertEqual(len(es), 2)
        self.assertAlmostEqual(es[0], (1 / 1.8 - 1 / 2) / (1 - 1 / 2), places=6)

    def test_is_constant(self):
        self.assertTrue(d.is_constant([0.10, 0.11, 0.09]))
        self.assertFalse(d.is_constant([0.10, 0.50]))

    def test_needs_varied_x(self):
        with self.assertRaises(ValueError):
            d.fit_fixed_variable([2, 2, 2], [1, 2, 3])

    def test_rates_must_be_positive(self):
        with self.assertRaises(ValueError):
            d.fit_fixed_variable([0, 1], [1, 2])

    def test_karp_flatt_requires_p_gt_1(self):
        with self.assertRaises(ValueError):
            d.karp_flatt([1.0], [1])


if __name__ == "__main__":
    unittest.main()
