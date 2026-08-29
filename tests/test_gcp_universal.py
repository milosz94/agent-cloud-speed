"""Universal GCP cost adapter: prove that discovery is EXHAUSTIVE (Cloud Asset Inventory lists EVERY
resource of ANY type) and that pricing is best-effort-WITH-DISCLOSURE (each discovered asset is priced
or added to unpriced_resources with its assetType + name, NEVER silently dropped or faked $0).

Offline: the enumerator's gcloud call is injected (run_cmd) and every SKU set is injected, so no live
Cloud Asset / Billing API is touched. The live Cloud Asset API is DISABLED on the dev project and enabling
it is out of scope; the injectable path + these tests are the deliverable.

The two headline proofs the task asks for:
  (a) a canned inventory with Cloud Run + Cloud SQL + a Compute disk + an UNKNOWN assetType prices/handles
      the known ones and DISCLOSES the disk and the unknown type BY NAME (never dropped);
  (b) a brand-new / unrecognized assetType is surfaced in unpriced_resources by name, never dropped.
"""
import unittest
from unittest import mock

from acspeed.adapters import gcp_cost as g


# --- SKU fixtures (injected; no live Catalog) --------------------------------------------------

def _run_sku(desc, regions, nanos):
    return {"description": desc, "serviceRegions": regions,
            "pricingInfo": [{"pricingExpression": {"tieredRates": [{"unitPrice": {"units": "0", "nanos": nanos}}]}}]}


def _sql_sku(desc, regions, nanos, group=None):
    s = {"description": desc, "serviceRegions": regions,
         "category": {"usageType": "OnDemand", "resourceFamily": "ApplicationServices"},
         "pricingInfo": [{"pricingExpression": {"tieredRates": [{"unitPrice": {"units": "0", "nanos": nanos}}]}}]}
    if group:
        s["category"]["resourceGroup"] = group
    return s


def _compute_sku(desc, regions, nanos, family="Compute"):
    return {"description": desc, "serviceRegions": regions,
            "category": {"usageType": "OnDemand", "resourceFamily": family},
            "pricingInfo": [{"pricingExpression": {"tieredRates": [{"unitPrice": {"units": "0", "nanos": nanos}}]}}]}


_RUN_SKUS = [
    _run_sku("Requests", ["global"], 400),
    _run_sku("Services CPU (Request-based billing)", ["europe-west1"], 24000),
    _run_sku("Services Memory (Request-based billing)", ["europe-west1"], 2500),
    _run_sku("Services Min Instance CPU (Request-based billing)", ["europe-west1"], 2500),
    _run_sku("Services Min Instance Memory (Request-based billing)", ["europe-west1"], 2500),
]

_SQL_SKUS = [
    _sql_sku("Cloud SQL for PostgreSQL: Zonal - Micro instance in EMEA", ["europe-west1"], 8000000),
    _sql_sku("Cloud SQL for PostgreSQL: Zonal - Enterprise Storage Hyperdisk Balanced Capacity in EMEA",
             ["europe-west1"], 170000000),
    _sql_sku("Cloud SQL for PostgreSQL: Zonal - IP address reservation in EMEA", ["europe-west1"],
             10000000, group="IpAddress"),
]

# N2 Core/Ram running SKUs (europe-west1) + a Balanced PD capacity SKU, for the real-pricing proofs.
_COMPUTE_SKUS = [
    _compute_sku("N2 Instance Core running in EMEA", ["europe-west1"], 20000000),   # $0.02 / vCPU-hr
    _compute_sku("N2 Instance Ram running in EMEA", ["europe-west1"], 3000000),     # $0.003 / GiB-hr
    _compute_sku("Balanced PD Capacity in EMEA", ["europe-west1"], 100000000, family="Storage"),  # $0.10 / GB-mo
]


# --- canned inventory items (what Cloud Asset Inventory returns; shape verified from gcloud help) ---

def _asset(asset_type, short, location="europe-west1", attrs=None):
    a = {"assetType": asset_type,
         "name": f"//x.googleapis.com/projects/redu-425516/locations/{location}/x/{short}",
         "displayName": short, "location": location}
    if attrs is not None:
        a["additionalAttributes"] = attrs
    return a


class TestUniversalEnumerator(unittest.TestCase):
    """The COMPLETE enumerator builds the correct gcloud Cloud Asset Inventory command and parses its
    JSON list; injectable so it runs offline."""

    def test_default_command_shape_and_parsing(self):
        seen = {}

        def run_cmd(args):
            seen["args"] = args
            return [{"assetType": "run.googleapis.com/Service", "name": "//.../services/umami"}]

        out = g.gcp_project_assets("projects/redu-425516", "name:acs1a2b3c", run_cmd=run_cmd)
        # exact command the default resolver runs (gcloud ... --format=json is appended by _gcloud_json):
        self.assertEqual(seen["args"],
                         ["asset", "search-all-resources", "--scope=projects/redu-425516",
                          "--query=name:acs1a2b3c"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["assetType"], "run.googleapis.com/Service")

    def test_asset_types_flag_is_added(self):
        seen = {}

        def run_cmd(args):
            seen["args"] = args
            return []

        g.gcp_project_assets("projects/p", "", run_cmd=run_cmd,
                             asset_types=["compute.googleapis.com/Disk", "storage.googleapis.com/Bucket"])
        self.assertIn("--asset-types=compute.googleapis.com/Disk,storage.googleapis.com/Bucket", seen["args"])
        self.assertNotIn("--query=", " ".join(seen["args"]))   # no empty query flag

    def test_non_list_output_is_empty_not_crash(self):
        self.assertEqual(g.gcp_project_assets("projects/p", "q", run_cmd=lambda a: None), [])
        self.assertEqual(g.gcp_project_assets("projects/p", "q", run_cmd=lambda a: {"err": 1}), [])

    def test_default_resolver_queries_by_run_token_and_is_gated_without_one(self):
        seen = []

        def run_cmd(args):
            seen.append(args)
            return []

        with mock.patch.object(g, "_adc_project", lambda: "redu-425516"):
            # run-token in the URL is the anchor and the ONLY thing the default resolver will sweep for
            g.gcp_assets_for_deployment("https://umami-acs1a2b3c-9988.europe-west1.run.app", run_cmd=run_cmd)
            # no token: the default (live) resolver cannot scope safely, so it returns [] WITHOUT a live call
            self.assertEqual(
                g.gcp_assets_for_deployment("https://umami-123456789.europe-west1.run.app", run_cmd=run_cmd), [])
        self.assertEqual(len(seen), 1)                       # only the run-token deploy triggered a query
        self.assertEqual(seen[0][2], "--scope=projects/redu-425516")
        self.assertEqual(seen[0][3], "--query=name:acs1a2b3c")

    def test_default_resolver_no_project_is_empty(self):
        with mock.patch.object(g, "_adc_project", lambda: None):
            self.assertEqual(g.gcp_assets_for_deployment("https://x.run.app"), [])


class TestDispatcherDiscloses(unittest.TestCase):
    """(a) + (b): every discovered asset is priced/handled OR disclosed by name; nothing is dropped."""

    def test_a_known_priced_disk_and_unknown_disclosed_by_name(self):
        assets = [
            _asset("run.googleapis.com/Service", "umami"),
            _asset("sqladmin.googleapis.com/Instance", "umami-db"),
            _asset("compute.googleapis.com/Disk", "leftover-disk", location="europe-west1-b"),  # no size -> disclosed
            _asset("pubsub.googleapis.com/Topic", "events-topic", location="global"),           # unknown -> disclosed
        ]
        sweep = g.price_discovered_assets(assets, url_region="europe-west1")

        # the two KNOWN types are recognized (routed to the fast-path pricers), never disclosed as unpriced
        priced_types = {p["asset_type"] for p in sweep.priced}
        self.assertIn("run.googleapis.com/Service", priced_types)
        self.assertIn("sqladmin.googleapis.com/Instance", priced_types)
        unpriced_types = {u["asset_type"] for u in sweep.unpriced_resources}
        self.assertNotIn("run.googleapis.com/Service", unpriced_types)
        self.assertNotIn("sqladmin.googleapis.com/Instance", unpriced_types)

        # the Compute disk and the unknown type are DISCLOSED, each carrying its assetType AND its name
        unpriced_by_type = {u["asset_type"]: u for u in sweep.unpriced_resources}
        self.assertIn("compute.googleapis.com/Disk", unpriced_by_type)
        self.assertIn("pubsub.googleapis.com/Topic", unpriced_by_type)
        self.assertIn("leftover-disk", unpriced_by_type["compute.googleapis.com/Disk"]["name"])
        self.assertIn("events-topic", unpriced_by_type["pubsub.googleapis.com/Topic"]["name"])

        # and the human-facing disclosure notes name both by their short name
        joined = " ".join(sweep.notes)
        self.assertIn("leftover-disk", joined)
        self.assertIn("events-topic", joined)
        self.assertIn("pubsub.googleapis.com/Topic", joined)

        # completeness: nothing was silently dropped -- every asset is either priced/handled or disclosed
        self.assertEqual(len(sweep.priced) + len(sweep.unpriced_resources), len(assets))

    def test_b_brand_new_type_is_surfaced_never_dropped(self):
        # a resource TYPE the tool has never seen -- caught with NO new code
        assets = [_asset("aiplatform.googleapis.com/FeatureOnlineStore", "brand-new-store")]
        sweep = g.price_discovered_assets(assets, url_region="us-central1")
        self.assertEqual(len(sweep.unpriced_resources), 1)
        u = sweep.unpriced_resources[0]
        self.assertEqual(u["asset_type"], "aiplatform.googleapis.com/FeatureOnlineStore")
        self.assertIn("brand-new-store", u["name"])
        self.assertEqual(sweep.floor_hourly_usd, 0.0)          # unpriced never fakes a dollar figure
        self.assertTrue(any("brand-new-store" in n for n in sweep.notes))
        # it must NOT appear as priced
        self.assertNotIn("aiplatform.googleapis.com/FeatureOnlineStore",
                         {p["asset_type"] for p in sweep.priced})

    def test_gcs_bucket_disclosed_as_usage_metered_never_zero(self):
        sweep = g.price_discovered_assets([_asset("storage.googleapis.com/Bucket", "user-uploads")],
                                          url_region="europe-west1")
        self.assertEqual(sweep.floor_hourly_usd, 0.0)
        self.assertEqual(sweep.unpriced_resources[0]["asset_type"], "storage.googleapis.com/Bucket")
        self.assertTrue(any("usage-metered" in n for n in sweep.notes))


class TestDispatcherPricesRealDollars(unittest.TestCase):
    """Best-effort pricing actually COMPUTES (reusing the existing Core/Ram + storage helpers) when the
    inventory carries the dimension and a SKU is injected -- proving 'price each', not only 'disclose'."""

    def test_compute_disk_prices_with_size_and_sku(self):
        disk = _asset("compute.googleapis.com/Disk", "data-disk", location="europe-west1-b",
                      attrs={"sizeGb": "10"})
        sweep = g.price_discovered_assets([disk], url_region="europe-west1", compute_skus=_COMPUTE_SKUS)
        self.assertEqual(len(sweep.unpriced_resources), 0)
        self.assertAlmostEqual(sweep.floor_hourly_usd, 0.10 * 10 / 730.0, places=6)   # GB-month -> $/hr
        self.assertEqual(sweep.priced[0]["asset_type"], "compute.googleapis.com/Disk")
        self.assertGreater(sweep.priced[0]["hourly_usd"], 0)

    def test_compute_instance_prices_from_family_core_ram(self):
        inst = _asset("compute.googleapis.com/Instance", "worker-vm", location="europe-west1-b",
                      attrs={"machineType": "projects/p/zones/europe-west1-b/machineTypes/n2-standard-2",
                             "guestCpus": 2, "memoryMb": 8192})
        sweep = g.price_discovered_assets([inst], url_region="europe-west1", compute_skus=_COMPUTE_SKUS)
        self.assertEqual(len(sweep.unpriced_resources), 0)
        # 2 vCPU * $0.02 + 8 GiB * $0.003 = $0.064 /hr, via the shared _gce_core_ram_rates helper
        self.assertAlmostEqual(sweep.floor_hourly_usd, 2 * 0.02 + 8 * 0.003, places=6)

    def test_compute_instance_without_size_is_disclosed_not_faked(self):
        # machineType present but no vCPU/RAM and no run_cmd describe -> DISCLOSED, never a guessed price
        inst = _asset("compute.googleapis.com/Instance", "mystery-vm", location="europe-west1-b",
                      attrs={"machineType": ".../machineTypes/n2-standard-2"})
        sweep = g.price_discovered_assets([inst], url_region="europe-west1", compute_skus=_COMPUTE_SKUS)
        self.assertEqual(sweep.floor_hourly_usd, 0.0)
        self.assertEqual(sweep.unpriced_resources[0]["asset_type"], "compute.googleapis.com/Instance")


class TestServerlessAdapterSweepIntegration(unittest.TestCase):
    """End to end through GcpRunRateAdapter: the URL fast path prices Cloud Run + Cloud SQL, and the
    injected universal sweep folds/discloses everything else -- a leftover disk and a Pub/Sub topic are
    surfaced in the estimate's assumptions, and the estimate still prices (never nulled)."""

    def _adapter_result(self):
        scaling = {"service": "umami", "region": "europe-west1", "min_scale": 1, "cpu": 1.0,
                   "mem_gib": 1.0, "instance_based": False}
        extras = ([{"region": "europe-west1", "tier": "db-f1-micro", "storage_gb": 10.0,
                    "ipv4_enabled": True}], 1)
        route = lambda sid, **k: _RUN_SKUS if sid == g.CLOUD_RUN_SERVICE_ID else _SQL_SKUS
        assets = [
            _asset("run.googleapis.com/Service", "umami"),
            _asset("sqladmin.googleapis.com/Instance", "umami-db"),
            _asset("compute.googleapis.com/Disk", "leftover-disk", location="europe-west1-b"),
            _asset("pubsub.googleapis.com/Topic", "events-topic", location="global"),
        ]
        with mock.patch.object(g, "_adc_token", lambda: "tok"), \
             mock.patch.object(g, "_adc_project", lambda: "proj"), \
             mock.patch.object(g, "catalog_skus_authed", route), \
             mock.patch.object(g, "gcp_serverless_extras", lambda run_cmd=None: extras):
            ad = g.GcpRunRateAdapter(resolve_bundle=lambda r: None, resolve_scaling=lambda r: scaling,
                                     resolve_assets=lambda r: assets)
            return ad.run_rate("https://umami-123456789.europe-west1.run.app", capture_date="2026-08-29")

    def test_sweep_discloses_extra_resources_and_still_prices(self):
        ur = self._adapter_result()
        self.assertIsNotNone(ur)                                # sweep disclosure never nulls the estimate
        joined = " ".join(ur.assumptions)
        self.assertIn("leftover-disk", joined)                  # the disk is disclosed by name
        self.assertIn("pubsub.googleapis.com/Topic", joined)    # the unknown type is disclosed by type
        self.assertIn("events-topic", joined)


class TestSweepDefaultOnButRunTokenGated(unittest.TestCase):
    """The sweep is default-ON (consistent with the Azure/AWS enumerators), but the default resolver
    self-limits to refs carrying the harness run-token and returns [] otherwise, so the standing suite
    stays hermetic (no gcloud spawn on a test stub). ACSPEED_GCP_ASSET_SWEEP=0 disables it explicitly."""

    def test_default_on_returns_the_live_resolver(self):
        ad = g.GcpRunRateAdapter(resolve_bundle=lambda r: None)
        import os
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop(g._ASSET_SWEEP_ENV, None)
            self.assertIs(g._resolve_assets_for(ad, "https://x.run.app"), g.gcp_assets_for_deployment)

    def test_env_flag_zero_disables_the_sweep(self):
        ad = g.GcpRunRateAdapter(resolve_bundle=lambda r: None)
        with mock.patch.dict("os.environ", {g._ASSET_SWEEP_ENV: "0"}):
            self.assertIsNone(g._resolve_assets_for(ad, "https://x.run.app"))

    def test_default_resolver_is_hermetic_without_a_run_token(self):
        # no run-token in the ref -> [] with no gcloud spawn (what keeps the default suite hermetic)
        spawned = []
        self.assertEqual(g.gcp_assets_for_deployment("dep", run_cmd=lambda a: spawned.append(a) or []), [])
        self.assertEqual(spawned, [])


if __name__ == "__main__":
    unittest.main()
