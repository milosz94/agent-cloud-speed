"""The standard low/medium/high traffic estimate (RunRate.traffic_estimate / UsageRate.traffic_estimate).

Every priced cost result carries a total-$/mo estimate at 10k, 500k, and 10M requests/mo, so a human
reading a run knows what to expect at their scale without decoding a schedule. A fixed-resource deploy
is flat across the tiers (a VM has no per-request charge); a usage-metered one reads its priced schedule.
It is surfaced in to_dict() (hence the recorded run JSON) and in the run log + tables.
"""
import unittest

from acspeed import cost
from acspeed.cost import RateComponent, UsageRate, UsagePoint, TRAFFIC_TIERS, REFERENCE_REQUEST_GRID


class TestTrafficEstimate(unittest.TestCase):
    def test_tiers_are_named_and_are_grid_points(self):
        self.assertEqual([n for n, _ in TRAFFIC_TIERS], ["low", "medium", "high"])
        # each tier must be a point on the schedule grid, or a usage rate could not price it exactly
        for _, reqs in TRAFFIC_TIERS:
            self.assertIn(reqs, REFERENCE_REQUEST_GRID)

    def test_standing_is_flat_across_tiers_and_in_dict(self):
        rr = cost.compose_run_rate(
            [RateComponent("compute", hourly_usd=0.10, raw_unit_price=0.10, native_unit="hour")],
            provider="p", region="r", flavor="f", capture_date="2026-08-29")
        m = round(rr.monthly_usd(), 2)
        te = rr.traffic_estimate()
        self.assertEqual(te, {"low": m, "medium": m, "high": m})   # no per-request charge -> flat
        self.assertEqual(rr.to_dict()["traffic_estimate"], te)     # exposed in the recorded dict

    def test_usage_reads_the_schedule_and_grows_with_traffic(self):
        ur = UsageRate(
            provider="p", region="r", service="serverless", capture_date="2026-08-29",
            components=(),
            schedule=(
                UsagePoint("10k", 10_000, 0.5, 8.00),
                UsagePoint("500k", 500_000, 25.0, 8.20),
                UsagePoint("10M", 10_000_000, 500.0, 12.00),
            ))
        te = ur.traffic_estimate()
        self.assertEqual(te, {"low": 8.00, "medium": 8.20, "high": 12.00})
        self.assertGreater(te["high"], te["low"])                  # traffic adds cost
        self.assertEqual(ur.to_dict()["traffic_estimate"], te)

    def test_usage_tier_absent_from_grid_is_none(self):
        # a rate priced on a partial grid (no 10M point) reports that tier as None, never a wrong number
        ur = UsageRate(provider="p", region="r", service="s", capture_date="d", components=(),
                       schedule=(UsagePoint("10k", 10_000, 0.5, 8.0),
                                 UsagePoint("500k", 500_000, 25.0, 8.2)))
        self.assertEqual(ur.traffic_estimate(), {"low": 8.0, "medium": 8.2, "high": None})


if __name__ == "__main__":
    unittest.main()
