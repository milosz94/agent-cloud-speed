"""GCP cost-coverage gaps (offline). Proves the Cloud Run always-on floor (request-based min-instance and
instance-based billing), the Cloud SQL public IPv4 charge, and the torn-down disclosure are all priced, with
NO live cloud: SKUs and the scaling / enumeration resolvers are injected.

Gaps closed here:
  GAP 1  Cloud Run min-instances floor (request-based) was omitted; now folded from the Min-Instance SKUs.
  GAP 2  --no-cpu-throttling deploys bill INSTANCE-based (no per-request Requests fee, compute per instance
         lifetime); the tool priced request-based and invented a phantom per-request fee.
  GAP 4  Cloud SQL public IPv4 (post-2024 in-use external IPv4) was uncharged; now added when ipv4Enabled.
  GAP 5  Artifact Registry image storage is disclosed (de-minimis, within the free tier), never silent.
"""
import json
import unittest
from unittest import mock

from acspeed.adapters import gcp_cost as g


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


# request-based active + Min-Instance idle + instance-based SKUs, plus Tier-2 min-instance decoys
_RUN_SKUS = [
    _run_sku("Requests", ["global"], 400),                                                      # 4e-7 / req
    _run_sku("Services CPU (Request-based billing)", ["europe-west1", "us-central1"], 24000),   # 2.4e-5 active
    _run_sku("Services Memory (Request-based billing)", ["europe-west1", "us-central1"], 2500),  # 2.5e-6 active
    _run_sku("Services Min Instance CPU (Request-based billing)", ["europe-west1", "us-central1"], 2500),   # idle
    _run_sku("Services Min Instance Memory (Request-based billing)", ["europe-west1", "us-central1"], 2500),  # idle
    _run_sku("Services Min Instance CPU Tier 2 (Request-based billing)", ["asia-east2"], 3500),      # decoy
    _run_sku("Services Min Instance Memory Tier 2 (Request-based billing)", ["asia-east2"], 3500),   # decoy
    _run_sku("Services CPU (Instance-based billing) in europe-west1", ["europe-west1"], 18000),   # 1.8e-5
    _run_sku("Services Memory (Instance-based billing) in europe-west1", ["europe-west1"], 2000),  # 2e-6
]

_SQL_SKUS = [
    _sql_sku("Cloud SQL for PostgreSQL: Zonal - Micro instance in EMEA", ["europe-west1"], 8000000),   # 0.008/hr
    _sql_sku("Cloud SQL for PostgreSQL: Zonal - Enterprise Storage Hyperdisk Balanced Capacity in EMEA",
             ["europe-west1"], 170000000),                                                             # 0.17/GB-mo
    _sql_sku("Cloud SQL for PostgreSQL: Zonal - IP address reservation in EMEA", ["europe-west1"],
             10000000, group="IpAddress"),                                                            # 0.01/hr
    _sql_sku("FDC Trial in Cloud SQL for PostgreSQL: Zonal - IP address reservation in EMEA",
             ["europe-west1"], 5000000, group="IpAddress"),                                           # excluded
]


class TestCloudRunScalingFloor(unittest.TestCase):
    """GAP 1 + GAP 2: the always-on Cloud Run compute floor, on the DELIBERATELY-matched Min-Instance and
    Instance-based SKUs the per-request path excludes."""

    def test_request_based_min_instance_floor(self):
        fl = g.cloud_run_scaling_floor_hourly(_RUN_SKUS, "europe-west1", min_scale=1, cpu=1, mem_gib=1,
                                              instance_based=False)
        # 1 * (1*2.5e-6 + 1*2.5e-6) * 3600 = 0.018 $/hr = $13.14/mo, on the Tier-1 Min-Instance SKUs
        self.assertAlmostEqual(fl[0], 0.018, places=6)
        self.assertAlmostEqual(fl[0] * 730, 13.14, places=2)
        self.assertAlmostEqual(fl[1], 2.5e-6)   # picked the Tier-1 idle SKU, not the Tier-2 decoy (3.5e-6)

    def test_instance_based_floor_uses_instance_skus(self):
        fl = g.cloud_run_scaling_floor_hourly(_RUN_SKUS, "europe-west1", min_scale=1, cpu=1, mem_gib=1,
                                              instance_based=True)
        # 1 * (1*1.8e-5 + 1*2e-6) * 3600 = 0.072 $/hr = $52.56/mo, on the Instance-based SKUs
        self.assertAlmostEqual(fl[0], 0.072, places=6)
        self.assertAlmostEqual(fl[0] * 730, 52.56, places=2)
        self.assertAlmostEqual(fl[1], 1.8e-5)

    def test_floor_scales_with_min_scale_and_size(self):
        fl = g.cloud_run_scaling_floor_hourly(_RUN_SKUS, "europe-west1", min_scale=2, cpu=2, mem_gib=4,
                                              instance_based=False)
        self.assertAlmostEqual(fl[0], 2 * (2 * 2.5e-6 + 4 * 2.5e-6) * 3600, places=8)

    def test_missing_sku_is_none_not_faked(self):
        no_ib = [s for s in _RUN_SKUS if "Instance-based" not in s["description"]]
        self.assertIsNone(g.cloud_run_scaling_floor_hourly(no_ib, "europe-west1", min_scale=1, cpu=1,
                                                           mem_gib=1, instance_based=True))


class TestInstanceBasedUsageRate(unittest.TestCase):
    """GAP 2: instance-based billing drops the per-request Requests fee AND the per-request active-compute
    fold (both are request-based concepts); the schedule is the instance-based standing floor, flat."""

    def test_omit_requests_drops_request_fee_and_is_flat(self):
        floor = 0.072   # the instance-based Cloud Run floor
        ur = g.cloud_run_usage_rate("2026-08-29", region="europe-west1", skus=_RUN_SKUS,
                                    standing_floor_hourly=floor, omit_requests=True)
        d = ur.to_dict()
        names = {c["name"] for c in d["components"]}
        self.assertNotIn("requests", names)                 # no phantom per-request fee under instance-based
        self.assertNotIn("compute-active-cpu", names)       # active compute is in the floor, not per-request
        sched = {p["requests_per_month"]: p["usd_per_month"] for p in d["schedule"]}
        self.assertEqual(sched[10_000], sched[10_000_000])  # flat: instances, not requests, drive the bill
        self.assertAlmostEqual(sched[10_000_000], floor * 730.0, places=2)
        self.assertIn("instance-based", d["price_source"])

    def test_request_based_keeps_requests_and_rises(self):
        ur = g.cloud_run_usage_rate("2026-08-29", region="europe-west1", skus=_RUN_SKUS,
                                    standing_floor_hourly=0.018, omit_requests=False)
        d = ur.to_dict()
        names = {c["name"] for c in d["components"]}
        self.assertIn("requests", names)
        sched = {p["requests_per_month"]: p["usd_per_month"] for p in d["schedule"]}
        self.assertGreater(sched[10_000_000], sched[10_000])   # the per-request schedule is kept on top


class TestCloudSqlIpv4(unittest.TestCase):
    """GAP 4: a public IPv4 on Cloud SQL adds the in-use external-IP charge; excluded when disabled."""

    def test_ip_added_when_enabled(self):
        no_ip = g.cloud_sql_hourly("europe-west1", "db-f1-micro", 10, skus=_SQL_SKUS, ipv4_enabled=False)
        with_ip = g.cloud_sql_hourly("europe-west1", "db-f1-micro", 10, skus=_SQL_SKUS, ipv4_enabled=True)
        self.assertAlmostEqual(with_ip[0] - no_ip[0], 0.01, places=6)   # exactly the $0.01/hr IP reservation
        self.assertAlmostEqual((with_ip[0] - no_ip[0]) * 730, 7.30, places=2)

    def test_ip_rate_excludes_fdc_trial(self):
        ipr = g._cloud_sql_ip_rate(_SQL_SKUS, "europe-west1")
        self.assertAlmostEqual(ipr[0], 0.01, places=6)   # the base SKU, not the cheaper FDC Trial decoy

    def test_ip_enabled_but_unpriceable_is_none_not_silent_zero(self):
        no_ip_sku = [s for s in _SQL_SKUS if "IP address reservation" not in s["description"]]
        self.assertIsNone(g.cloud_sql_hourly("europe-west1", "db-f1-micro", 10, skus=no_ip_sku,
                                             ipv4_enabled=True))
        # without a public IP the same SKU set prices fine (the guard does not over-refuse)
        self.assertIsNotNone(g.cloud_sql_hourly("europe-west1", "db-f1-micro", 10, skus=no_ip_sku,
                                                ipv4_enabled=False))


class TestScalingResolver(unittest.TestCase):
    """The injectable Cloud Run scaling resolver parses minScale, cpu, memory and the cpu-throttling mode."""

    @staticmethod
    def _list(url="https://umami-123456789.europe-west1.run.app", min_scale="1", throttling="false",
              cpu="1", memory="1Gi", region="europe-west1"):
        """Mock `gcloud run services list`: one service whose status.url serves `url` (ground-truth match),
        instead of the old URL-string parse that broke on the .a.run.app form."""
        def run_cmd(args):
            if args[:3] == ["run", "services", "list"]:
                return [{
                    "metadata": {"name": "umami-123456789", "labels": {"cloud.googleapis.com/location": region}},
                    "status": {"url": url},
                    "spec": {"template": {
                        "metadata": {"annotations": {"autoscaling.knative.dev/minScale": min_scale,
                                                     "run.googleapis.com/cpu-throttling": throttling}},
                        "spec": {"containers": [{"resources": {"limits": {"cpu": cpu, "memory": memory}}}]}}},
                }]
            return None
        return run_cmd

    def test_parses_instance_based(self):
        s = g._cloud_run_scaling_resolver(run_cmd=self._list(throttling="false"))(
            "https://umami-123456789.europe-west1.run.app")
        self.assertEqual(s["min_scale"], 1)
        self.assertEqual(s["cpu"], 1.0)
        self.assertEqual(s["mem_gib"], 1.0)
        self.assertTrue(s["instance_based"])
        self.assertEqual(s["region"], "europe-west1")

    def test_parses_request_based_default_throttling(self):
        s = g._cloud_run_scaling_resolver(run_cmd=self._list(throttling="true", memory="512Mi"))(
            "https://umami-123456789.europe-west1.run.app")
        self.assertFalse(s["instance_based"])
        self.assertEqual(s["mem_gib"], 0.5)

    def test_new_a_run_app_url_matches_by_inventory(self):
        # run12/14 defect: Cloud Run's newer <svc>-<hash>-<regioncode>.a.run.app URL made the OLD parser read
        # region 'a' + a wrong service name, so `describe` failed on every attempt and the min-instances floor
        # was silently dropped ($16 not $29). Matching status.url from the inventory is URL-format-agnostic.
        url = "https://umami-acs547c2aaa-aggnv775ja-uc.a.run.app"
        s = g._cloud_run_scaling_resolver(run_cmd=self._list(url=url, min_scale="1", throttling="true",
                                                             region="us-central1"))(url)
        self.assertIsNotNone(s, "the .a.run.app URL must resolve via inventory match, not URL parsing")
        self.assertEqual(s["min_scale"], 1)              # floor applied, not silently 0
        self.assertEqual(s["region"], "us-central1")
        self.assertFalse(s["instance_based"])

    def test_matches_when_recorded_url_differs_from_api_status_url(self):
        # verified LIVE (2026-08-30): a Cloud Run service has MULTIPLE URLs -- the deploy CLI prints the classic
        # <name>-<projnum>.<region>.run.app while the API's status.url is the new <name>-<hash>-<rc>.a.run.app.
        # acspeed may have recorded either, so matching must succeed via the shared service NAME, not the host.
        def run_cmd(args):
            if args[:3] == ["run", "services", "list"]:
                return [{
                    "metadata": {"name": "umami-acs547c2aaa",
                                 "labels": {"cloud.googleapis.com/location": "us-central1"}},
                    "status": {"url": "https://umami-acs547c2aaa-aggnv775ja-uc.a.run.app"},   # NEW form
                    "spec": {"template": {
                        "metadata": {"annotations": {"autoscaling.knative.dev/minScale": "1",
                                                     "run.googleapis.com/cpu-throttling": "true"}},
                        "spec": {"containers": [{"resources": {"limits": {"cpu": "1", "memory": "512Mi"}}}]}}},
                }]
            return None
        s = g._cloud_run_scaling_resolver(run_cmd=run_cmd)(   # query the CLASSIC form (different host)
            "https://umami-acs547c2aaa-299813327652.us-central1.run.app")
        self.assertIsNotNone(s, "must match on service name when the recorded URL != the API status.url")
        self.assertEqual(s["min_scale"], 1)
        self.assertEqual(s["region"], "us-central1")

    def test_longest_name_prefix_wins(self):
        # two services share a name prefix; the token-suffixed one must win, not bare 'umami'
        def base(name, ms):
            return {"metadata": {"name": name, "labels": {"cloud.googleapis.com/location": "us-central1"}},
                    "status": {"url": "https://%s-x-uc.a.run.app" % name},
                    "spec": {"template": {"metadata": {"annotations": {"autoscaling.knative.dev/minScale": ms}},
                             "spec": {"containers": [{"resources": {"limits": {"cpu": "1", "memory": "512Mi"}}}]}}}}
        def run_cmd(args):
            return [base("umami", "0"), base("umami-acs547c2aaa", "1")] if args[:3] == ["run", "services", "list"] else None
        s = g._cloud_run_scaling_resolver(run_cmd=run_cmd)(
            "https://umami-acs547c2aaa-299813327652.us-central1.run.app")
        self.assertEqual(s["service"], "umami-acs547c2aaa")   # not the bare 'umami'
        self.assertEqual(s["min_scale"], 1)

    def test_no_matching_service_returns_none(self):
        # the listing has no service serving this URL (truly gone), so after retrying the resolver yields None
        # and the caller discloses a 0 floor rather than inventing one.
        self.assertIsNone(g._cloud_run_scaling_resolver(run_cmd=lambda a: [])(
            "https://umami-1.europe-west1.run.app"))
        self.assertIsNone(g._cloud_run_scaling_resolver(run_cmd=lambda a: None)(
            "https://umami-1.europe-west1.run.app"))

    def test_transient_list_blip_is_retried_then_resolves(self):
        # the service is live at cost time, so an empty listing is a transient blip; it must be retried, not
        # read as gone (a dropped floor understates the bill $29 -> $16).
        good = self._list(min_scale="1", throttling="true")
        calls = {"n": 0}
        def flaky(args):
            calls["n"] += 1
            return None if calls["n"] <= 2 else good(args)   # blip twice, then the real listing
        s = g._cloud_run_scaling_resolver(run_cmd=flaky)("https://umami-123456789.europe-west1.run.app")
        self.assertIsNotNone(s, "a transient listing blip must be retried, not treated as gone")
        self.assertEqual(s["min_scale"], 1)
        self.assertGreaterEqual(calls["n"], 3)               # it retried past the two failures

    def test_hash_url_has_no_service(self):
        self.assertIsNone(g._cloud_run_service_region_from_url("https://abc123hash.run.app"))


class TestMemCpuParsers(unittest.TestCase):
    def test_cpu(self):
        self.assertEqual(g._parse_run_cpu("1000m"), 1.0)
        self.assertEqual(g._parse_run_cpu("2"), 2.0)
        self.assertEqual(g._parse_run_cpu("500m"), 0.5)
        self.assertIsNone(g._parse_run_cpu(""))

    def test_mem(self):
        self.assertEqual(g._parse_run_mem_gib("1Gi"), 1.0)
        self.assertEqual(g._parse_run_mem_gib("512Mi"), 0.5)
        self.assertEqual(g._parse_run_mem_gib("2Gi"), 2.0)
        self.assertIsNone(g._parse_run_mem_gib(""))


class TestServerlessRunRateEndToEnd(unittest.TestCase):
    """Full run_rate over the audited architecture (min-instances=1, 1 vCPU + 1 GiB, db-f1-micro + 10GB,
    public IPv4), offline: SKUs and enumeration injected. Everything prices (no UNPRICED)."""

    def _run(self, instance_based):
        scaling = {"service": "umami", "region": "europe-west1", "min_scale": 1, "cpu": 1.0,
                   "mem_gib": 1.0, "instance_based": instance_based}
        extras = ([{"region": "europe-west1", "tier": "db-f1-micro", "storage_gb": 10.0,
                    "ipv4_enabled": True}], 1)
        route = lambda sid, **k: _RUN_SKUS if sid == g.CLOUD_RUN_SERVICE_ID else _SQL_SKUS
        with mock.patch.object(g, "_adc_token", lambda: "tok"), \
             mock.patch.object(g, "_adc_project", lambda: "proj"), \
             mock.patch.object(g, "catalog_skus_authed", route), \
             mock.patch.object(g, "gcp_serverless_extras", lambda run_cmd=None: extras):
            ad = g.GcpRunRateAdapter(resolve_bundle=lambda r: None, resolve_scaling=lambda r: scaling)
            return ad.run_rate("https://umami-123456789.europe-west1.run.app", capture_date="2026-08-29")

    def test_request_based_prices_and_folds_all_gaps(self):
        ur = self._run(instance_based=False)
        self.assertIsNotNone(ur)                            # no UNPRICED for the real architecture
        d = ur.to_dict()
        te = d["traffic_estimate"]
        self.assertIsNotNone(te["low"])
        self.assertGreater(te["high"], te["low"])           # request-based schedule rises with traffic
        # idle floor = Cloud Run min-inst 0.018 + Cloud SQL (0.008 + 10*0.17/730 + 0.01 IP)
        floor = 0.018 + (0.008 + 10 * 0.17 / 730.0 + 0.01)
        self.assertAlmostEqual(te["low"], round(floor * 730 + 10_000 * 4e-7 + _folded_compute(10_000), 2),
                               delta=0.05)
        joined = " ".join(d["assumptions"])
        self.assertIn("min-instances", joined)
        self.assertIn("public IPv4", joined)
        self.assertIn("Artifact Registry", joined)

    def test_instance_based_prices_flat_no_request_fee(self):
        ur = self._run(instance_based=True)
        self.assertIsNotNone(ur)
        d = ur.to_dict()
        te = d["traffic_estimate"]
        self.assertEqual(te["low"], te["high"])             # flat: instances, not requests, drive the bill
        floor = 0.072 + (0.008 + 10 * 0.17 / 730.0 + 0.01)
        self.assertAlmostEqual(te["high"], round(floor * 730, 2), delta=0.05)
        joined = " ".join(d["assumptions"])
        self.assertIn("instance-based billing", joined)
        self.assertIn("public IPv4", joined)


class TestTornDownFloorDisclosed(unittest.TestCase):
    """A torn-down deploy (scaling config unavailable) uses a 0 Cloud Run floor but MUST disclose it, never
    silently omit a min-instances cost."""

    def test_disclosed_note_when_scaling_unavailable(self):
        extras = ([], 0)
        route = lambda sid, **k: _RUN_SKUS if sid == g.CLOUD_RUN_SERVICE_ID else _SQL_SKUS
        with mock.patch.object(g, "_adc_token", lambda: "tok"), \
             mock.patch.object(g, "_adc_project", lambda: "proj"), \
             mock.patch.object(g, "catalog_skus_authed", route), \
             mock.patch.object(g, "gcp_serverless_extras", lambda run_cmd=None: extras):
            ad = g.GcpRunRateAdapter(resolve_bundle=lambda r: None, resolve_scaling=lambda r: None)
            ur = ad.run_rate("https://umami-1.europe-west1.run.app", capture_date="2026-08-29")
        self.assertIsNotNone(ur)
        self.assertTrue(any("scaling config was unavailable" in a for a in ur.assumptions))


class V2ServiceResourceIsReadable(unittest.TestCase):
    """cloud_run_scaling_from_v2_service is the provenance the two re-priced GCP Easy records name.

    Those records say their scaling configuration was "read from the Cloud Run Admin v2 Service resource
    recorded in this run's own published transcript". A reader who follows that sentence lands on this
    function, so it has to be exercised: shipped with no caller and no test, the provenance line would
    point at code nothing runs. The fixtures below are the two Service resources as the transcripts
    record them, trimmed to the fields the resolver reads.
    """

    RUN12 = {"name": "projects/redu-425516/locations/us-central1/services/umami-acs547c2aaa",
             "uri": "https://umami-acs547c2aaa-aggnv775ja-uc.a.run.app",
             "template": {"scaling": {"minInstanceCount": 1, "maxInstanceCount": 3},
                          "containers": [{"resources": {"limits": {"cpu": "1", "memory": "1Gi"},
                                                        "startupCpuBoost": True}}]}}

    def test_reads_region_scale_and_size_off_the_v2_resource(self):
        got = g.cloud_run_scaling_from_v2_service(self.RUN12)
        self.assertEqual(got["region"], "us-central1")     # from the resource name, never the URL
        self.assertEqual(got["service"], "umami-acs547c2aaa")
        self.assertEqual(got["min_scale"], 1)
        self.assertEqual(got["cpu"], 1.0)
        self.assertEqual(got["mem_gib"], 1.0)

    def test_cpu_idle_absent_with_resources_set_is_instance_based(self):
        # Google's v2 reference: cpuIdle is "true by default. However, if ResourceRequirements is set,
        # the caller must explicitly set this field to true to preserve the default behavior."
        self.assertTrue(g.cloud_run_scaling_from_v2_service(self.RUN12)["instance_based"])

    def test_cpu_idle_true_is_request_based(self):
        doc = json.loads(json.dumps(self.RUN12))
        doc["template"]["containers"][0]["resources"]["cpuIdle"] = True
        self.assertFalse(g.cloud_run_scaling_from_v2_service(doc)["instance_based"])

    def test_the_a_run_app_url_yields_no_region_so_the_resource_name_is_the_only_source(self):
        # the defect this whole path exists for: the parse returned "a", which is not a region
        self.assertIsNone(g.cloud_run_region_from_url(self.RUN12["uri"]))
        self.assertEqual(g.cloud_run_region_from_url(
            "https://umami-123456789.us-central1.run.app"), "us-central1")


def _folded_compute(reqs):
    # the per-request active-compute fold the request-based schedule adds on top of the request fee, using
    # the module's disclosed default request profile (1 vCPU + 0.5 GiB held 0.1s per request)
    from acspeed import cost
    cpu = 2.4e-5 * cost.DEFAULT_REQUEST_VCPUS * cost.DEFAULT_REQUEST_SECONDS
    mem = 2.5e-6 * cost.DEFAULT_REQUEST_GIB * cost.DEFAULT_REQUEST_SECONDS
    return (cpu + mem) * reqs


if __name__ == "__main__":
    unittest.main()
