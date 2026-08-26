"""Part 4 cost axis: the standing hourly run-rate (acspeed/cost.py + redu_cost.py, CR C19)."""
import unittest

from acspeed import cost
from acspeed.cost import RateComponent, EgressRate, HOURS_PER_MONTH


class TestComposition(unittest.TestCase):
    def test_storage_gb_month_to_hourly(self):
        # $0.10/GB-month over 50 GB -> (0.10*50)/730 $/hr
        self.assertAlmostEqual(cost.storage_gb_month_to_hourly(0.10, 50), 0.10 * 50 / 730.0)

    def test_all_in_is_sum_and_egress_is_separate(self):
        comps = [
            RateComponent("compute", hourly_usd=0.20, raw_unit_price=0.20, native_unit="instance-hour"),
            RateComponent("storage", hourly_usd=cost.storage_gb_month_to_hourly(0.10, 50),
                          raw_unit_price=0.10, native_unit="GB-month", quantity=50),
            RateComponent("public_ip", hourly_usd=0.005, raw_unit_price=0.005, native_unit="hour"),
        ]
        egress = EgressRate(per_gb_usd=0.09, tier="first 10 TB/mo out")
        rr = cost.compose_run_rate(comps, provider="redu", region="uk-london", flavor="m1.medium",
                                   capture_date="2026-08-26", egress=egress)
        expected = 0.20 + (0.10 * 50 / 730.0) + 0.005
        self.assertAlmostEqual(rr.all_in_hourly_usd, round(expected, 6))
        # egress is NOT folded into the baseline
        self.assertNotAlmostEqual(rr.all_in_hourly_usd, expected + 0.09)
        self.assertEqual(rr.egress.per_gb_usd, 0.09)
        self.assertEqual(rr.egress.tier, "first 10 TB/mo out")
        # monthly = hourly x 730
        self.assertAlmostEqual(rr.monthly_usd(), rr.all_in_hourly_usd * HOURS_PER_MONTH)
        # exclusions stated by default; egress marked not-in-baseline in the dict
        self.assertIn("spot", rr.exclusions)
        self.assertFalse(rr.to_dict()["egress"]["in_baseline"])
        self.assertEqual(rr.to_dict()["all_in_hourly_usd"], rr.all_in_hourly_usd)

    def test_negative_component_rejected(self):
        with self.assertRaises(ValueError):
            RateComponent("compute", hourly_usd=-0.1, raw_unit_price=-0.1, native_unit="instance-hour")


class TestReduAdapter(unittest.TestCase):
    def _bundle(self):
        return {
            "flavor": "m1.medium", "region": "uk-london",
            "compute_hourly_usd": 0.20,
            "storage_gb": 50, "storage_usd_per_gb_month": 0.10,
            "public_ip_hourly_usd": 0.005,
            "ancillary": [], "egress": {"per_gb_usd": 0.09, "tier": "first 10 TB/mo out"},
            "price_source": "redu public on-demand list", "price_urls": [],
        }

    def test_adapter_composes_from_injected_bundle(self):
        from acspeed.adapters.redu_cost import ReduRunRateAdapter
        b = self._bundle()
        rr = ReduRunRateAdapter(resolve_bundle=lambda ref: b).run_rate("dep1", capture_date="2026-08-26")
        self.assertIsNotNone(rr)
        self.assertEqual(rr.provider, "redu")
        self.assertEqual(rr.flavor, "m1.medium")
        names = [c.name for c in rr.components]
        self.assertEqual(names, ["compute", "storage", "public_ip"])
        self.assertAlmostEqual(rr.all_in_hourly_usd,
                               round(0.20 + 0.10 * 50 / 730.0 + 0.005, 6))
        self.assertEqual(rr.egress.per_gb_usd, 0.09)      # egress carried separately
        self.assertEqual(rr.capture_date, "2026-08-26")   # dated, passed in (no clock read)

    def test_adapter_returns_none_when_unpriceable(self):
        from acspeed.adapters.redu_cost import ReduRunRateAdapter
        rr = ReduRunRateAdapter(resolve_bundle=lambda ref: None).run_rate("dep1", capture_date="2026-08-26")
        self.assertIsNone(rr)                             # disclosed, not faked


if __name__ == "__main__":
    unittest.main()
