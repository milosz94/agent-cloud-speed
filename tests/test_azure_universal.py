"""Azure UNIVERSAL resource-group sweep (offline, no cloud, no network, no credentials).

The pre-existing azure_cost adapter priced only the two types it had a branch for (Container Apps +
Postgres Flexible). Anything else the agent provisioned (a Storage account, a Redis cache, a public IP,
an ACR, a Log Analytics workspace, or a brand-new ARM type) was silently missed. The fix re-architects
the cost path to be COMPLETE: each acspeed run creates its OWN resource group ``rg-<run_token>`` and the
sweep lists EVERY resource in it (``az resource list -g <rg> -o json``), then routes each to a pricer BY
ITS ARM TYPE. Discovery is exhaustive; pricing is best-effort-with-disclosure, so nothing is a silent $0.

These tests prove, entirely offline with an INJECTED ``az resource list`` JSON and an INJECTED retail
``fetch`` (``az`` may be unauthenticated in this environment, so the injectable path is the deliverable):

  (a) a canned RG with Container Apps + Postgres + a Storage account + an unknown type prices the known
      ones and DISCLOSES the storage + the unknown type BY NAME (never a silent drop);
  (b) a brand-new / unrecognized ARM type is surfaced in ``unpriced_resources``, never dropped;
  (c) the ``rg-<run_token>`` derivation from a Container Apps URL works.

No em-dash-family characters anywhere (repo convention; asserted at the bottom)."""
import unittest

from acspeed.adapters import azure_cost
from acspeed.adapters.azure_cost import (AzureRunRateAdapter, azure_rg_resources,
                                         price_rg_resources, resource_group_from_url,
                                         run_token_from_ref)


# --- injected public Retail Prices payloads (the ONLY pricing input; no network) ---------------------
# Postgres Flexible Server compute (B1ms) + storage rows; a Redis per-hour cache meter. The fake fetch
# routes by the serviceName carried in the OData $filter URL (urllib quotes spaces as %20).
_PG_ITEMS = {"Items": [
    {"meterName": "B1ms Compute", "unitOfMeasure": "1 Hour", "retailPrice": 0.02086,
     "type": "Consumption", "productName": "Azure Database for PostgreSQL Flexible Server",
     "skuName": "B1ms"},
    {"meterName": "Storage Data Stored", "unitOfMeasure": "1 GB/Month", "retailPrice": 0.115,
     "type": "Consumption", "productName": "Azure Database for PostgreSQL Flex Server Storage",
     "skuName": "Storage"},
]}
_REDIS_ITEMS = {"Items": [
    {"meterName": "C1 Cache", "unitOfMeasure": "1 Hour", "retailPrice": 0.055,
     "type": "Consumption", "productName": "Standard", "skuName": "C1"},
]}


def _fake_retail(url: str):
    u = (url or "").lower()
    if "postgresql" in u:
        return _PG_ITEMS
    if "redis" in u:
        return _REDIS_ITEMS
    return {"Items": []}


def _accounted(sweep):
    """Every visited resource lands in exactly ONE bucket (priced / serverless / disclosed): the
    completeness invariant. Returns the total count accounted for."""
    return len(sweep.components) + len(sweep.serverless_types) + len(sweep.unpriced_resources)


class TestUniversalDispatch(unittest.TestCase):
    """(a) + (b): the dispatcher prices known types and DISCLOSES everything else by name."""

    def _mixed_rg(self):
        # a realistic Container Apps deploy: the app + its managed DB + a Storage account it created +
        # a resource TYPE the tool has never anticipated. All carry this run's token in their name.
        return [
            {"type": "Microsoft.App/containerApps", "name": "umami-acs1a2b3c4d", "location": "westeurope"},
            {"type": "Microsoft.DBforPostgreSQL/flexibleServers", "name": "pg-acs1a2b3c4d",
             "location": "westeurope", "sku": {"name": "Standard_B1ms", "tier": "Burstable"},
             "properties": {"storage": {"storageSizeGB": 32}}},
            {"type": "Microsoft.Storage/storageAccounts", "name": "stacs1a2b3c4d",
             "location": "westeurope", "sku": {"name": "Standard_LRS"}},
            {"type": "Microsoft.Quantum/quantumWorkspaces", "name": "qt-acs1a2b3c4d",
             "location": "westeurope"},
        ]

    def test_a_prices_known_discloses_storage_and_unknown_by_name(self):
        sweep = price_rg_resources(self._mixed_rg(), region="westeurope", fetch=_fake_retail)

        # Postgres is PRICED (a real standing line with a positive rate)
        pg = [c for c in sweep.components if c.name == "compute:postgres-flexible"]
        self.assertEqual(len(pg), 1, sweep.components)
        self.assertGreater(pg[0].hourly_usd, 0.0)

        # Container Apps is RECOGNIZED (priced by the usage schedule), not dumped into unpriced
        self.assertTrue(any("containerApps" in s for s in sweep.serverless_types), sweep.serverless_types)
        self.assertFalse(any("containerApps" in u for u in sweep.unpriced_resources), sweep.unpriced_resources)

        # the Storage account is DISCLOSED by its ARM type AND its name (usage-priced, not a silent $0)
        self.assertTrue(any("Microsoft.Storage/storageAccounts" in u and "stacs1a2b3c4d" in u
                            for u in sweep.unpriced_resources), sweep.unpriced_resources)

        # the never-anticipated type is DISCLOSED by its full ARM type string AND its name
        self.assertTrue(any("Microsoft.Quantum/quantumWorkspaces" in u and "qt-acs1a2b3c4d" in u
                            for u in sweep.unpriced_resources), sweep.unpriced_resources)

        # completeness: all four discovered resources are accounted for, none dropped
        self.assertEqual(_accounted(sweep), 4)

    def test_b_brand_new_type_is_surfaced_never_dropped(self):
        # a type with NO pricer and NO table row must still be surfaced, by its exact ARM type string
        resources = [{"type": "Microsoft.NewCategory/futureThings", "name": "ft-acs99abcd",
                      "location": "westeurope"}]
        sweep = price_rg_resources(resources, region="westeurope")
        self.assertEqual(sweep.components, ())            # nothing priced
        self.assertEqual(len(sweep.unpriced_resources), 1)  # but it is NOT dropped
        self.assertIn("Microsoft.NewCategory/futureThings", sweep.unpriced_resources[0])
        self.assertIn("ft-acs99abcd", sweep.unpriced_resources[0])
        self.assertEqual(_accounted(sweep), 1)

    def test_recognized_but_unpriceable_type_is_still_disclosed(self):
        # a Redis with no matching retail meter (empty fetch): recognized, cannot be priced -> DISCLOSED,
        # never silently dropped. This is the best-effort-with-disclosure contract.
        resources = [{"type": "Microsoft.Cache/Redis", "name": "rd-acs1a2b3c4d", "location": "westeurope",
                      "sku": {"name": "Standard", "family": "C", "capacity": 1}}]
        sweep = price_rg_resources(resources, region="westeurope", fetch=lambda url: {"Items": []})
        self.assertEqual(sweep.components, ())
        self.assertTrue(any("Microsoft.Cache/Redis" in u and "not priceable" in u
                            for u in sweep.unpriced_resources), sweep.unpriced_resources)

    def test_recognized_priceable_redis_is_priced(self):
        # same Redis WITH a matching per-hour cache meter injected: it is priced as a standing line
        resources = [{"type": "Microsoft.Cache/Redis", "name": "rd-acs1a2b3c4d", "location": "westeurope",
                      "sku": {"name": "Standard", "family": "C", "capacity": 1}}]
        sweep = price_rg_resources(resources, region="westeurope", fetch=_fake_retail)
        redis = [c for c in sweep.components if c.name == "compute:redis"]
        self.assertEqual(len(redis), 1, sweep.unpriced_resources)
        self.assertAlmostEqual(redis[0].hourly_usd, 0.055)

    def test_skip_types_avoids_double_count(self):
        # Container Apps + Postgres are priced by the serverless path; the sweep must SKIP them so they
        # are neither double-counted nor disclosed as unpriced when skipped.
        sweep = price_rg_resources(
            self._mixed_rg(), region="westeurope", fetch=_fake_retail,
            skip_types=(azure_cost._TYPE_CONTAINERAPPS, azure_cost._TYPE_POSTGRES_FLEX))
        self.assertEqual(sweep.components, ())            # postgres skipped, so no priced line here
        self.assertEqual(sweep.serverless_types, ())      # container apps skipped
        # only the storage account + the unknown type remain, both disclosed
        self.assertEqual(len(sweep.unpriced_resources), 2, sweep.unpriced_resources)


class TestResourceGroupDerivation(unittest.TestCase):
    """(c): rg-<run_token> derived from a Container Apps URL / run token."""

    def test_c_rg_from_container_apps_url(self):
        self.assertEqual(
            resource_group_from_url(
                "https://umami-acs1a2b3c4d.happyhill.westeurope.azurecontainerapps.io"),
            "rg-acs1a2b3c4d")

    def test_c_rg_when_token_is_the_whole_first_segment(self):
        self.assertEqual(
            resource_group_from_url("acs1a2b3c4d.env-id.westeurope.azurecontainerapps.io"),
            "rg-acs1a2b3c4d")

    def test_c_no_token_returns_none_never_a_wrong_rg(self):
        # no token in the host: return None (disclosed upstream), never guess another run's RG
        self.assertIsNone(resource_group_from_url(
            "https://umami.happyhill.westeurope.azurecontainerapps.io"))

    def test_run_token_ignores_short_hex_lookalikes(self):
        # "acs" followed by too-few hex chars is not the 8-hex run token, so it must not false-match
        self.assertIsNone(run_token_from_ref("https://acsca.westeurope.azurecontainerapps.io"))
        self.assertEqual(run_token_from_ref("https://x-acsdeadbeef.westeurope.azurecontainerapps.io"),
                         "acsdeadbeef")


class TestInjectableEnumerator(unittest.TestCase):
    """The complete enumerator runs `az resource list -g <rg> -o json`, injectable for offline tests."""

    def test_default_enumerator_command_and_parsing(self):
        seen = {}

        def fake_az(args):
            seen["args"] = args
            return [{"type": "Microsoft.Storage/storageAccounts", "name": "st1"},
                    {"type": "Microsoft.App/containerApps", "name": "app1"}]

        resources = azure_rg_resources("rg-acs1a2b3c4d", run_cmd=fake_az)
        # it runs exactly `az resource list -g rg-acs1a2b3c4d` (the -o json is added by _az_json)
        self.assertEqual(seen["args"], ["resource", "list", "-g", "rg-acs1a2b3c4d"])
        self.assertEqual(len(resources), 2)
        self.assertEqual(resources[0]["type"], "Microsoft.Storage/storageAccounts")

    def test_empty_rg_makes_no_call(self):
        called = {"n": 0}

        def fake_az(args):
            called["n"] += 1
            return []

        self.assertEqual(azure_rg_resources("", run_cmd=fake_az), [])
        self.assertEqual(called["n"], 0)   # a missing RG never shells out

    def test_az_failure_returns_empty_not_faked(self):
        # az unauthenticated / not installed -> run_cmd yields None -> [] (non-fatal, disclosed upstream)
        self.assertEqual(azure_rg_resources("rg-x", run_cmd=lambda args: None), [])


class TestAdapterIntegration(unittest.TestCase):
    """End to end through run_rate on a Container Apps URL: the sweep folds priced extras into the
    standing floor and DISCLOSES the rest, all reachable via the injected resource list (no cloud)."""

    _CA_ALL = {"Items": [
        {"meterName": "Standard Requests", "unitOfMeasure": "1M", "retailPrice": 0.56,
         "type": "Consumption", "productName": "Azure Container Apps"},
        {"meterName": "Standard vCPU Active Usage", "unitOfMeasure": "1 Second", "retailPrice": 3.4e-5,
         "type": "Consumption", "productName": "Azure Container Apps"},
        {"meterName": "Standard Memory Active Usage", "unitOfMeasure": "1 GiB Second", "retailPrice": 4e-6,
         "type": "Consumption", "productName": "Azure Container Apps"},
    ]}

    def _fetch(self, url):
        u = (url or "").lower()
        if "container%20apps" in u or "container apps" in u:
            return self._CA_ALL
        return _fake_retail(url)

    def test_sweep_disclosures_reach_the_usage_rate_and_adapter(self):
        rg = [
            {"type": "Microsoft.App/containerApps", "name": "umami-acs1a2b3c4d", "location": "westeurope"},
            {"type": "Microsoft.Storage/storageAccounts", "name": "stacs1a2b3c4d",
             "location": "westeurope", "sku": {"name": "Standard_LRS"}},
            {"type": "Microsoft.Quantum/quantumWorkspaces", "name": "qt-acs1a2b3c4d",
             "location": "westeurope"},
        ]
        adapter = AzureRunRateAdapter(
            postgres_resolver=lambda ref: [],                   # no managed DB in this deploy
            scale_resolver=lambda ref: None,                    # scale not enumerable (kept focused)
            rg_resources_resolver=lambda ref: rg)               # the INJECTED complete inventory
        orig = azure_cost._http_get_json
        azure_cost._http_get_json = self._fetch
        try:
            ur = adapter.run_rate(
                "https://umami-acs1a2b3c4d.happyhill.westeurope.azurecontainerapps.io",
                capture_date="2026-08-28")
        finally:
            azure_cost._http_get_json = orig

        self.assertIsNotNone(ur)
        d = ur.to_dict()
        # the storage account + the unknown type are disclosed in the schedule's assumptions, by name
        self.assertTrue(any("Microsoft.Storage/storageAccounts" in a for a in d["assumptions"]),
                        d["assumptions"])
        self.assertTrue(any("Microsoft.Quantum/quantumWorkspaces" in a for a in d["assumptions"]),
                        d["assumptions"])
        # and they are also reachable on the adapter for the caller (like AWS's disclosures)
        self.assertTrue(any("storageAccounts" in u for u in adapter.rg_unpriced), adapter.rg_unpriced)
        self.assertTrue(any("quantumWorkspaces" in u for u in adapter.rg_unpriced), adapter.rg_unpriced)


class TestNoEmDash(unittest.TestCase):
    def test_no_em_dash_family_chars(self):
        import acspeed.adapters.azure_cost as mod
        targets = [mod.__file__, __file__]
        for path in targets:
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            for cp in (0x2014, 0x2013, 0x2015):   # em / en / horizontal-bar, by code point not literal
                self.assertNotIn(chr(cp), src, f"em-dash-family character in {path}")


if __name__ == "__main__":
    unittest.main()
