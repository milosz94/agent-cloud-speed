"""Part 4 cost axis: the standing hourly run-rate (acspeed/cost.py + redu_cost.py, CR C19)."""
import os
import unittest

from acspeed import cost
from acspeed.cost import RateComponent, EgressRate, FxConversion, HOURS_PER_MONTH

# the REAL redu MCP list_flavors shape (prices under `hourly_rate`, IN GBP; the flavor bundles its disk)
REDU_FLAVORS = [
    {"id": "1", "name": "m1.tiny", "vcpus": 1, "ram": 512, "disk": 1, "hourly_rate": 0.0118},
    {"id": "2", "name": "m1.small", "vcpus": 1, "ram": 2048, "disk": 20, "hourly_rate": 0.0174},
    {"id": "3", "name": "m1.medium", "vcpus": 2, "ram": 4096, "disk": 40, "hourly_rate": 0.0278},
    {"id": "4", "name": "m1.large", "vcpus": 4, "ram": 8192, "disk": 80, "hourly_rate": 0.0486},
    {"id": "5", "name": "m1.xlarge", "vcpus": 8, "ram": 16384, "disk": 160, "hourly_rate": 0.0764},
]


def _fake_call_tool(deps, flavors, detail=None, dbs=None, redis=None, rdbs=None):
    """Mimic redu_mcp_http.call_tool for list_deployments / list_flavors / get_deployment and the
    managed-datastore list tools (list_databases / list_relational_databases / list_redis)."""
    def call(name, args):
        if name == "list_deployments":
            return {"deployments": deps}
        if name == "list_flavors":
            return {"flavors": flavors}
        if name == "list_databases":
            return {"databases": dbs or []}
        if name == "list_relational_databases":
            return {"relational_databases": rdbs or []}
        if name == "list_redis":
            return {"redis": redis or []}
        if name == "get_deployment":
            if detail is not None:
                return detail
            return next((d for d in deps if str(d.get("id")) == str(args.get("id"))), {})
        return {}
    return call


_FIXED_FX = lambda ccy, date: ((1.27, "2026-08-26", "test-fx") if ccy == "GBP" else None)


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


class TestReduRealShapeAndGbpFx(unittest.TestCase):
    """The default resolver against the REAL MCP shapes + the dated GBP->USD conversion (CR C19)."""

    def _adapter(self, deps, detail=None, fx=_FIXED_FX, dbs=None, redis=None, rdbs=None):
        from acspeed.adapters.redu_cost import ReduRunRateAdapter
        return ReduRunRateAdapter(
            call_tool=_fake_call_tool(deps, REDU_FLAVORS, detail=detail, dbs=dbs, redis=redis, rdbs=rdbs),
            fx=fx)

    def test_prices_from_hourly_rate_and_converts_gbp_to_usd(self):
        # flavor named on the deployment; redu prices m1.large at 0.0486 GBP/hr, disk bundled
        rr = self._adapter([{"id": 557, "flavor": "m1.large", "region": "uk-london"}]) \
            .run_rate(557, capture_date="2026-08-26")
        self.assertIsNotNone(rr)
        self.assertEqual([c.name for c in rr.components], ["compute"])   # bundled disk -> no storage line
        comp = rr.components[0]
        self.assertAlmostEqual(comp.raw_unit_price, 0.0486)             # native GBP figure disclosed
        self.assertAlmostEqual(rr.all_in_hourly_usd, round(0.0486 * 1.27, 6))
        # FX disclosed: native GBP, USD reporting, the dated rate + source
        self.assertIsNotNone(rr.fx)
        self.assertEqual((rr.fx.native_currency, rr.fx.reporting_currency), ("GBP", "USD"))
        self.assertAlmostEqual(rr.fx.rate, 1.27)
        d = rr.to_dict()
        self.assertEqual(d["fx"]["native_currency"], "GBP")
        self.assertAlmostEqual(d["all_in_hourly_usd"], round(0.0486 * 1.27, 6))

    def test_flavor_matches_by_id_not_only_name(self):
        # deployment carries the flavor id ("4"), not its name -- must still resolve m1.large
        rr = self._adapter([{"id": 700, "flavor_id": "4", "region": "uk-london"}]) \
            .run_rate(700, capture_date="2026-08-26")
        self.assertIsNotNone(rr)
        self.assertAlmostEqual(rr.all_in_hourly_usd, round(0.0486 * 1.27, 6))

    def test_get_deployment_fallback_supplies_flavor(self):
        # the list row has no flavor; get_deployment (by id) does
        deps = [{"id": 557, "region": "uk-london"}]
        detail = {"id": 557, "flavor": "m1.medium", "region": "uk-london"}
        rr = self._adapter(deps, detail=detail).run_rate(557, capture_date="2026-08-26")
        self.assertIsNotNone(rr)
        self.assertEqual(rr.flavor, "m1.medium")
        self.assertAlmostEqual(rr.all_in_hourly_usd, round(0.0278 * 1.27, 6))

    def test_unpriceable_when_fx_unavailable(self):
        rr = self._adapter([{"id": 557, "flavor": "m1.large"}], fx=lambda c, d: None) \
            .run_rate(557, capture_date="2026-08-26")
        self.assertIsNone(rr)                             # no dated rate -> disclosed, not faked

    def test_unpriceable_when_flavor_absent(self):
        rr = self._adapter([{"id": 557, "flavor": "does-not-exist"}]) \
            .run_rate(557, capture_date="2026-08-26")
        self.assertIsNone(rr)

    def test_two_vm_deploy_prices_app_plus_managed_db(self):
        # the docmost shape: app m1.medium (flavor_id 3) + a managed Postgres m1.small (flavor_id 2)
        # named by db_id. Both are billed; the app-VM-only price under-counted by ~40%.
        dep = {"id": 558, "flavor_id": "3", "region": "uk-london", "db_id": "191",
               "redis_id": None, "media_space_id": None, "floating_ip": "10.50.0.184"}
        dbs = [{"id": "191", "flavor_id": "2", "ha": False, "member_ips": None}]
        rr = self._adapter([dep], dbs=dbs).run_rate(558, capture_date="2026-08-26")
        self.assertIsNotNone(rr)
        self.assertEqual([c.name for c in rr.components], ["compute", "compute:postgres"])
        # all-in = (app 0.0278 + postgres 0.0174) GBP/hr x 1.27, NOT the app VM alone
        self.assertAlmostEqual(rr.all_in_hourly_usd, round((0.0278 + 0.0174) * 1.27, 6))
        self.assertGreater(rr.all_in_hourly_usd, round(0.0278 * 1.27, 6))   # strictly more than app-only
        pg = next(c for c in rr.components if c.name == "compute:postgres")
        self.assertAlmostEqual(pg.raw_unit_price, 0.0174)                   # native GBP figure disclosed

    def test_ha_managed_db_counts_members_as_instance_hours(self):
        # an HA managed DB is a 3-member cluster: priced as 3 instance-hours, not one
        dep = {"id": 559, "flavor_id": "3", "region": "uk-london", "db_id": "200"}
        dbs = [{"id": "200", "flavor_id": "2", "ha": True,
                "member_ips": ["10.1.0.1", "10.1.0.2", "10.1.0.3"]}]
        rr = self._adapter([dep], dbs=dbs).run_rate(559, capture_date="2026-08-26")
        pg = next(c for c in rr.components if c.name == "compute:postgres")
        self.assertEqual(pg.quantity, 3.0)                                  # members disclosed
        self.assertAlmostEqual(pg.hourly_usd, 0.0174 * 3 * 1.27)
        self.assertAlmostEqual(rr.all_in_hourly_usd, round((0.0278 + 0.0174 * 3) * 1.27, 6))

    def test_managed_redis_priced_as_its_own_resource(self):
        dep = {"id": 560, "flavor_id": "3", "region": "uk-london", "redis_id": "77"}
        redis = [{"id": "77", "flavor_id": "1", "ha": False}]               # m1.tiny 0.0118
        rr = self._adapter([dep], redis=redis).run_rate(560, capture_date="2026-08-26")
        self.assertEqual([c.name for c in rr.components], ["compute", "compute:redis"])
        self.assertAlmostEqual(rr.all_in_hourly_usd, round((0.0278 + 0.0118) * 1.27, 6))

    def test_unpriceable_managed_resource_is_disclosed_not_faked(self):
        # a db_id that resolves to no row is DISCLOSED in price_source (never a silent zero), and the
        # app is still priced (only the load-bearing app compute failing yields None)
        dep = {"id": 561, "flavor_id": "3", "region": "uk-london", "db_id": "999"}
        rr = self._adapter([dep], dbs=[], rdbs=[]).run_rate(561, capture_date="2026-08-26")
        self.assertIsNotNone(rr)
        self.assertEqual([c.name for c in rr.components], ["compute"])
        self.assertIn("excludes unpriced: database", rr.to_dict()["price_source"])


class TestDatedFx(unittest.TestCase):
    def test_usd_is_identity(self):
        from acspeed.adapters.redu_cost import _dated_to_usd
        self.assertEqual(_dated_to_usd("USD", "2026-08-26"), (1.0, "2026-08-26", "identity"))

    def test_env_override_pins_the_rate_offline(self):
        from acspeed.adapters.redu_cost import _dated_to_usd
        os.environ["ACSPEED_FX_GBP_USD"] = "1.30"
        try:
            rate, rate_date, source = _dated_to_usd("GBP", "2026-08-26")
        finally:
            del os.environ["ACSPEED_FX_GBP_USD"]
        self.assertEqual(rate, 1.30)
        self.assertIn("env:", source)

    def test_fetch_failure_is_none_not_faked(self):
        from acspeed.adapters.redu_cost import _dated_to_usd
        self.assertIsNone(_dated_to_usd("GBP", "2026-08-26", fetch=lambda c, d: None))

    def test_fx_conversion_rejects_nonpositive_rate(self):
        with self.assertRaises(ValueError):
            FxConversion("GBP", "USD", 0.0, "2026-08-26", "x")


if __name__ == "__main__":
    unittest.main()
