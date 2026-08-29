"""Azure cost-coverage gaps (offline, no cloud, no network).

Covers the two audited gaps in ``acspeed/adapters/azure_cost.py``:

GAP 6 - Container Apps min-replica IDLE compute floor. With minReplicas >= 1 one replica runs 24/7 (not
scale-to-zero), so Azure bills the DISTINCT idle meters (Standard vCPU/Memory Idle Usage) on top of the
active per-request usage. The tool must resolve minReplicas + per-replica cpu/memory and fold
minReplicas x (cpu x idleVcpu + memGiB x idleMem) x 3600 into the standing floor; 0 when minReplicas == 0.

GAP 7 - the managed-Postgres standing floor must come from an INJECTABLE resolver, and when the resolver
finds no DB the code must DISCLOSE that (a note) rather than silently emit a DB-less number as complete.

Everything is injected: a fake retail ``fetch``, a fake scale resolver, a fake postgres resolver. No em-dash
characters anywhere (repo test bans them)."""
import unittest

from acspeed.adapters import azure_cost


# --- fake public Retail Prices payload for 'Azure Container Apps' -------------------------------------
# Both the ACTIVE meters (used by the existing schedule) and the DISTINCT IDLE meters (GAP 6). Prices are
# the live-verified 2026-08-28 westeurope figures.
_CA_ACTIVE = [
    {"meterName": "Standard Requests", "unitOfMeasure": "1M", "retailPrice": 0.56,
     "type": "Consumption", "productName": "Azure Container Apps"},
    {"meterName": "Standard vCPU Active Usage", "unitOfMeasure": "1 Second", "retailPrice": 3.4e-5,
     "type": "Consumption", "productName": "Azure Container Apps"},
    {"meterName": "Standard Memory Active Usage", "unitOfMeasure": "1 GiB Second", "retailPrice": 4e-6,
     "type": "Consumption", "productName": "Azure Container Apps"},
]
_CA_IDLE = [
    {"meterName": "Standard vCPU Idle Usage", "unitOfMeasure": "1 Second", "retailPrice": 4e-6,
     "type": "Consumption", "productName": "Azure Container Apps"},
    {"meterName": "Standard Memory Idle Usage", "unitOfMeasure": "1 GiB Second", "retailPrice": 4e-6,
     "type": "Consumption", "productName": "Azure Container Apps"},
]
_CA_ALL = {"Items": _CA_ACTIVE + _CA_IDLE}

_IDLE_VCPU_S = 4e-6
_IDLE_MEM_S = 4e-6
_HOURS_PER_MONTH = 730.0


def _floor_month(hourly):
    return hourly * _HOURS_PER_MONTH


class TestIdleFloorFold(unittest.TestCase):
    """GAP 6: the always-on idle replica floor is added when minReplicas >= 1, and is 0 when minReplicas == 0."""

    def _rate(self, scale, standing_floor=0.0):
        return azure_cost.container_apps_usage_rate(
            "2026-08-28", region="westeurope", standing_floor_hourly=standing_floor,
            scale=scale, fetch=lambda url: _CA_ALL)

    def test_idle_floor_added_when_min_replicas_one(self):
        # 1 replica x (1 vCPU x idleVcpu + 2 GiB x idleMem) x 3600 per hour, x 730 per month
        scale = {"min_replicas": 1, "cpu": 1.0, "memory_gib": 2.0}
        expected_idle_hourly = 1 * (1.0 * _IDLE_VCPU_S + 2.0 * _IDLE_MEM_S) * 3600.0
        self.assertAlmostEqual(expected_idle_hourly, 0.0432)   # sanity: matches the hand-computed figure

        ur = self._rate(scale).to_dict()
        # the schedule's low tier (10k requests) is dominated by the floor; subtract the tiny per-request
        # charge to recover the pure monthly floor and confirm the idle floor is in it.
        at_10k = next(p["usd_per_month"] for p in ur["schedule"] if p["requests_per_month"] == 10_000)
        # with no other floor, the monthly floor is exactly the idle floor
        self.assertGreaterEqual(at_10k, _floor_month(expected_idle_hourly))
        # and it is disclosed
        self.assertTrue(any("idle floor" in a.lower() and "minReplicas=1" in a for a in ur["assumptions"]),
                        ur["assumptions"])

    def test_idle_floor_stacks_on_db_floor(self):
        # a non-zero DB standing floor and the idle floor must BOTH be in the monthly floor
        scale = {"min_replicas": 1, "cpu": 1.0, "memory_gib": 2.0}
        idle_hourly = 1 * (1.0 * _IDLE_VCPU_S + 2.0 * _IDLE_MEM_S) * 3600.0
        db_hourly = 0.02590109589041096                     # B1ms + 32GB westeurope, live-verified
        ur = self._rate(scale, standing_floor=db_hourly)
        # monthly floor = (idle + db) x 730; read the 10k tier and subtract the per-request charge upper bound
        at_10k = ur.monthly_at(10_000)
        self.assertAlmostEqual(at_10k, _floor_month(idle_hourly + db_hourly), delta=0.5)
        # strictly larger than either floor alone
        self.assertGreater(at_10k, _floor_month(idle_hourly))
        self.assertGreater(at_10k, _floor_month(db_hourly))

    def test_idle_floor_zero_when_scale_to_zero(self):
        scale = {"min_replicas": 0, "cpu": 1.0, "memory_gib": 2.0}
        ur = self._rate(scale).to_dict()
        at_10k = next(p["usd_per_month"] for p in ur["schedule"] if p["requests_per_month"] == 10_000)
        # no always-on replica: the only monthly cost at 10k is the tiny per-request charge, well under $1
        self.assertLess(at_10k, 1.0)
        self.assertTrue(any("scale-to-zero" in a.lower() for a in ur["assumptions"]), ur["assumptions"])

    def test_min_replicas_one_but_idle_meters_missing_is_unpriced(self):
        # a real always-on charge we cannot price must return None (UNPRICED), never a silent understatement
        active_only = {"Items": list(_CA_ACTIVE)}
        scale = {"min_replicas": 1, "cpu": 1.0, "memory_gib": 2.0}
        self.assertIsNone(azure_cost.container_apps_usage_rate(
            "2026-08-28", region="westeurope", scale=scale, fetch=lambda url: active_only))

    def test_scale_unknown_is_disclosed_not_silently_zero(self):
        # scale None (app not enumerable): still price active usage, but disclose the idle floor is omitted
        ur = azure_cost.container_apps_usage_rate(
            "2026-08-28", region="westeurope", scale=None, fetch=lambda url: _CA_ALL).to_dict()
        self.assertTrue(any("idle floor unavailable" in a.lower() for a in ur["assumptions"]), ur["assumptions"])


class TestGap7PostgresDisclosure(unittest.TestCase):
    """GAP 7: the Postgres floor comes from an INJECTABLE resolver, and the no-DB case appends a disclosure
    note instead of silently using floor 0."""

    def _adapter(self, servers, scale=None):
        # inject the postgres resolver (ref -> servers, run-token-scoped like azure_postgres_extras) and a
        # scale resolver so no cloud is touched. Scale None -> no idle floor, keeping this test on the DB.
        return azure_cost.AzureRunRateAdapter(
            postgres_resolver=lambda ref: servers,
            scale_resolver=lambda ref: scale)

    def _run(self, servers, scale=None):
        # patch the module-level retail fetch so the serverless branch prices offline
        orig = azure_cost._http_get_json
        azure_cost._http_get_json = lambda url: _postgres_or_ca(url)
        try:
            return self._adapter(servers, scale).run_rate(
                "https://umami.happyhill.westeurope.azurecontainerapps.io", capture_date="2026-08-28")
        finally:
            azure_cost._http_get_json = orig

    def test_no_db_appends_disclosure_note_and_still_prices_active(self):
        ur = self._run(servers=[])
        self.assertIsNotNone(ur)
        d = ur.to_dict()
        self.assertTrue(any("db standing floor unavailable" in a.lower() for a in d["assumptions"]),
                        d["assumptions"])

    def test_found_db_folds_into_floor_no_disclosure_note(self):
        servers = [{"region": "westeurope", "sku": "Standard_B1ms", "storage_gb": 32.0}]
        ur = self._run(servers=servers)
        self.assertIsNotNone(ur)
        d = ur.to_dict()
        self.assertFalse(any("db standing floor unavailable" in a.lower() for a in d["assumptions"]),
                         d["assumptions"])
        # the DB floor (~0.0259/hr x 730 ~= $18.9/mo) is now inside the 10k-tier monthly cost
        at_10k = ur.monthly_at(10_000)
        self.assertGreater(at_10k, 15.0)

    def test_found_but_unpriceable_db_returns_none(self):
        # a DB the deploy provisioned but we cannot price -> UNPRICED (completeness guard preserved)
        servers = [{"region": "westeurope", "sku": "", "storage_gb": 32.0}]   # empty sku -> unpriceable
        self.assertIsNone(self._run(servers=servers))

    def test_idle_and_db_floor_both_folded_through_run_rate(self):
        # end-to-end: real DB + minReplicas=1 idle floor both present in the schedule
        servers = [{"region": "westeurope", "sku": "Standard_B1ms", "storage_gb": 32.0}]
        scale = {"min_replicas": 1, "cpu": 1.0, "memory_gib": 2.0}
        ur = self._run(servers=servers, scale=scale)
        self.assertIsNotNone(ur)
        d = ur.to_dict()
        self.assertTrue(any("idle floor" in a.lower() and "minReplicas=1" in a for a in d["assumptions"]),
                        d["assumptions"])
        idle_hourly = 1 * (1.0 * _IDLE_VCPU_S + 2.0 * _IDLE_MEM_S) * 3600.0
        db_hourly = 0.02590109589041096
        at_10k = ur.monthly_at(10_000)
        self.assertAlmostEqual(at_10k, _floor_month(idle_hourly + db_hourly), delta=0.5)


# --- offline retail payloads for the run_rate end-to-end path ----------------------------------------
# Postgres Flexible Server compute (B1ms) + storage rows, and the Container Apps rows, keyed by which
# serviceName the OData $filter names. The real fetch is patched to return the right one per URL.
_PG_ITEMS = {"Items": [
    {"meterName": "B1ms Compute", "unitOfMeasure": "1 Hour", "retailPrice": 0.02086,
     "type": "Consumption", "productName": "Azure Database for PostgreSQL Flexible Server",
     "skuName": "B1ms"},
    {"meterName": "Storage Data Stored", "unitOfMeasure": "1 GB/Month", "retailPrice": 0.115,
     "type": "Consumption", "productName": "Azure Database for PostgreSQL Flex Server Storage",
     "skuName": "Storage"},
]}


def _postgres_or_ca(url: str):
    u = (url or "").lower()
    if "container%20apps" in u or "container apps" in u:
        return _CA_ALL
    if "postgresql" in u:
        return _PG_ITEMS
    return {"Items": []}


class TestAppServicePath(unittest.TestCase):
    """Azure App Service (*.azurewebsites.net) is a STANDING App Service Plan, not Container Apps. The agent
    picks `az webapp up` (App Service) or `az containerapp up` (Container Apps) non-deterministically; before
    this path an App Service URL fell through to the VM resolver and returned UNPRICED (ok=False) - a real
    deploy silently unpriced. It must price the plan tier + the managed Postgres floor as a run-rate."""

    _RETAIL = [
        {"serviceName": "Azure App Service", "productName": "Azure App Service Basic Plan - Linux",
         "skuName": "B1", "meterName": "B1", "unitOfMeasure": "1 Hour", "retailPrice": 0.017,
         "type": "Consumption", "armRegionName": "westus2"},
        {"serviceName": "Azure App Service", "productName": "Azure App Service Basic Plan - Windows",
         "skuName": "B1", "meterName": "B1 Windows", "unitOfMeasure": "1 Hour", "retailPrice": 0.075,
         "type": "Consumption", "armRegionName": "westus2"},   # must be dropped (Windows)
        {"serviceName": "Azure Database for PostgreSQL",
         "productName": "Azure Database for PostgreSQL Flexible Server Burstable BS Series Compute",
         "skuName": "B1MS", "meterName": "B1MS", "unitOfMeasure": "1 Hour", "retailPrice": 0.0199,
         "type": "Consumption"},
        {"serviceName": "Azure Database for PostgreSQL",
         "productName": "Azure Database for PostgreSQL Flex Server Storage", "meterName": "Storage Data Stored",
         "unitOfMeasure": "1 GB/Month", "retailPrice": 0.1369, "type": "Consumption"},
    ]

    # az resource list entries for an App Service deploy (plan + web app + postgres), in ANY resource group
    _RES = [
        {"name": "asp-umami-acs1a2b", "type": "Microsoft.Web/serverfarms", "location": "westus2",
         "sku": {"name": "B1"}, "resourceGroup": "rg-umami-acs1a2b"},
        {"name": "umami-acs1a2b", "type": "Microsoft.Web/sites", "location": "westus2",
         "resourceGroup": "rg-umami-acs1a2b"},
        {"name": "umami-db-acs1a2b", "type": "Microsoft.DBforPostgreSQL/flexibleServers", "location": "westus2",
         "sku": {"name": "Standard_B1ms"}, "properties": {"storage": {"storageSizeGb": 32}},
         "resourceGroup": "rg-umami-acs1a2b"},
    ]

    def test_plan_pricer_is_a_data_table_row(self):
        # App Service is priced by the sweep's _RG_DISPATCH data table, NOT a per-type run_rate branch
        self.assertIs(azure_cost._RG_DISPATCH["microsoft.web/serverfarms"], azure_cost._price_app_service_plan)
        comp = azure_cost._price_app_service_plan(self._RES[0], "westus2", fetch=lambda u: {"Items": self._RETAIL})
        self.assertIsNotNone(comp)
        self.assertAlmostEqual(comp.hourly_usd, 0.017, places=4)        # Linux B1; the Windows meter is dropped
        self.assertIsNone(azure_cost._price_app_service_plan({"sku": {"name": "P3v9"}}, "westus2",
                                                             fetch=lambda u: {"Items": self._RETAIL}))

    def test_token_discovery_finds_resources_in_any_rg(self):
        # rg is rg-umami-acs1a2b (NOT rg-acs1a2b) - token discovery must still find it by name/RG match
        def run_cmd(args):
            return self._RES if args[:2] == ["resource", "list"] else None
        got = azure_cost.azure_resources_for_token("acs1a2b", run_cmd)
        self.assertEqual(len(got), 3)

    def test_run_rate_sums_the_swept_standing_lines(self):
        from unittest import mock
        # the universal path: inject the discovered resources; the sweep prices App Service + Postgres
        ad = azure_cost.AzureRunRateAdapter(rg_resources_resolver=lambda ref: self._RES)
        with mock.patch.object(azure_cost, "_retail_query", return_value=self._RETAIL):
            rr = ad.run_rate("https://umami-acs1a2b.azurewebsites.net", capture_date="2026-08-29")
        self.assertIsNotNone(rr)                                        # NOT ok=False / UNPRICED anymore
        d = rr.to_dict()
        self.assertEqual(d["kind"], "standing")
        names = [c["name"] for c in d["components"]]
        self.assertTrue(any("app-service-plan" in n for n in names))
        self.assertTrue(any("postgres" in n for n in names))
        self.assertGreater(d["monthly_usd"], 25.0)                     # plan ~12.4 + DB ~19 = ~31/mo
        self.assertTrue(any("Microsoft.Web/sites" in u for u in ad.rg_unpriced))   # web app disclosed, not dropped

    def test_no_resources_falls_back_and_discloses(self):
        # empty sweep + no VM bundle -> UNPRICED (None), never a faked number
        ad = azure_cost.AzureRunRateAdapter(rg_resources_resolver=lambda ref: [],
                                            resolve_bundle=lambda ref: None)
        self.assertIsNone(ad.run_rate("https://x.azurewebsites.net", capture_date="2026-08-29"))


class TestNoEmDash(unittest.TestCase):
    def test_source_has_no_em_dash(self):
        import acspeed.adapters.azure_cost as mod
        with open(mod.__file__, "r", encoding="utf-8") as f:
            src = f.read()
        for cp in (0x2014, 0x2013, 0x2015):   # em / en / horizontal-bar, by code point not literal
            self.assertNotIn(chr(cp), src, "em-dash-family character in azure_cost.py")


if __name__ == "__main__":
    unittest.main()
