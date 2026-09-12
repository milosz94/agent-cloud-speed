"""Driver (autorun) regressions: the readiness predicate and the concurrent external poller.

Both pinned by the 2026-08-25 Isso run: the 200-399 rule on '/' called a documented 400 an outage
(24 min recorded for a 3 min deploy), and the clock could not stop before the agent's own 'done'
because polling only began after the session ended. No network: is_serving is stubbed.
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import autorun
from autorun import pick_url


class TestServingPredicate(unittest.TestCase):
    def test_application_responses_serve(self):
        # 4xx on a blind root is the application answering (Isso 400, Umami 3xx, API 401, SPA 404)
        for code in ("200", "204", "301", "302", "308", "400", "401", "403", "404", "418", "499"):
            self.assertTrue(autorun.serving_predicate(code), code)

    def test_absent_or_erroring_origin_does_not_serve(self):
        # 000 = no TLS/TCP (no proxy host / DNS yet); 5xx = gateway with no upstream or the app's own error
        for code in ("000", "500", "502", "503", "504", "599"):
            self.assertFalse(autorun.serving_predicate(code), code)

    def test_garbage_does_not_serve(self):
        for code in ("", "abc", "20", "2000", "-1"):
            self.assertFalse(autorun.serving_predicate(code), repr(code))

    def test_isso_regression_400_is_serving(self):
        self.assertTrue(autorun.serving_predicate("400"))

    def test_predicate_string_matches_rule(self):
        self.assertIn("< 500", autorun.SERVING_PREDICATE)


REDU_URL_RE = r"https://[a-z0-9.-]+\.redu\.cloud"
REDU_SUBSTRATE = (r"^https://(?:mcp|api|console|dashboard|docs|www|register)\.redu\.cloud"
                  r"|^https://redu\.cloud/?$")


class TestPickUrlSubstrate(unittest.TestCase):
    """The cloud's own control-plane hosts must never be picked as the deployed app.

    Regression pinned 2026-08-25 (jotty run): redu.md's bootstrap line
    `claude mcp add ... https://mcp.redu.cloud/mcp` put mcp.redu.cloud (always 200) into the
    transcript; the poller latched it and stopped the clock at 20s before deploy_compose ran."""

    def test_mcp_host_is_excluded(self):
        text = ("bootstrap: claude mcp add --transport http redu https://mcp.redu.cloud/mcp\n"
                "Deployment #532 created: https://jotty-cro9jhtr.redu.cloud")
        pick = pick_url(text, REDU_URL_RE, REDU_SUBSTRATE)
        self.assertEqual(pick["url"], "https://jotty-cro9jhtr.redu.cloud")
        self.assertNotIn("https://mcp.redu.cloud", pick["candidates"])

    def test_all_platform_hosts_excluded(self):
        for h in ("mcp", "api", "console", "dashboard", "docs", "www", "register"):
            text = f"see https://{h}.redu.cloud and the app https://myapp-x7.redu.cloud"
            self.assertEqual(pick_url(text, REDU_URL_RE, REDU_SUBSTRATE)["url"],
                             "https://myapp-x7.redu.cloud", h)

    def test_bare_apex_excluded_but_app_subdomain_kept(self):
        text = "origin https://redu.cloud, app https://blog-abc.redu.cloud"
        self.assertEqual(pick_url(text, REDU_URL_RE, REDU_SUBSTRATE)["url"], "https://blog-abc.redu.cloud")

    def test_no_substrate_re_keeps_old_behaviour(self):
        text = "https://mcp.redu.cloud and https://app-x.redu.cloud"
        self.assertEqual(pick_url(text, REDU_URL_RE)["candidates"],
                         ["https://mcp.redu.cloud", "https://app-x.redu.cloud"])

    def test_adapter_config_has_substrate_hosts(self):
        self.assertIn("substrate_hosts", autorun.ADAPTERS["redu"])
        import re as _re
        sub = _re.compile(autorun.ADAPTERS["redu"]["substrate_hosts"])
        self.assertTrue(sub.search("https://mcp.redu.cloud"))
        self.assertFalse(sub.search("https://jotty-cro9jhtr.redu.cloud"))


class TestResolveOutDir(unittest.TestCase):
    """Results are grouped by cloud: <app>/acspeed-results/<adapter>/, and the results tree is never
    copied into the agent's working copy."""

    def test_default_groups_by_cloud(self):
        for adapter in ("redu", "aws", "gcp", "azure"):
            out, ign = autorun.resolve_out_dir("/home/x/app", adapter, None)
            self.assertEqual(out, f"/home/x/app/acspeed-results/{adapter}")
            self.assertEqual(ign, ["acspeed-results"])   # skip the whole results tree, not just the cloud dir

    def test_two_clouds_are_sibling_dirs(self):
        redu, _ = autorun.resolve_out_dir("/home/x/app", "redu", None)
        aws, _ = autorun.resolve_out_dir("/home/x/app", "aws", None)
        self.assertEqual(os.path.dirname(redu), os.path.dirname(aws))
        self.assertNotEqual(redu, aws)

    def test_custom_out_inside_app_is_ignored_from_copy(self):
        out, ign = autorun.resolve_out_dir("/home/x/app", "redu", "/home/x/app/myout")
        self.assertEqual(out, "/home/x/app/myout")
        self.assertEqual(ign, ["myout"])

    def test_custom_out_outside_app_has_no_ignore(self):
        out, ign = autorun.resolve_out_dir("/home/x/app", "redu", "/tmp/elsewhere")
        self.assertEqual(out, "/tmp/elsewhere")
        self.assertEqual(ign, [])


class TestAwsUrlWhitelist(unittest.TestCase):
    """The AWS adapter must pick only real app fronts, never AWS service/API endpoints.

    Pinned because a broad `amazonaws.com` pattern matches ec2/sts/s3/ecr endpoints, which all answer
    <500 to a bare GET and would falsely stop the clock at ~0s (the mcp.redu.cloud defect on AWS)."""

    URL_RE = None
    SUB = None

    @classmethod
    def setUpClass(cls):
        cls.URL_RE = autorun.ADAPTERS["aws"]["url_re"]
        cls.SUB = autorun.ADAPTERS["aws"]["substrate_hosts"]

    def _cands(self, text):
        return pick_url(text, self.URL_RE, self.SUB)["candidates"]

    def test_app_fronts_are_picked(self):
        for u in ("https://abc123xyz.us-east-1.awsapprunner.com",
                  "http://ec2-3-8-1-2.us-east-1.compute.amazonaws.com:8080",
                  "http://ec2-54-1-2-3.compute-1.amazonaws.com",
                  "https://my-alb-1234567.us-east-1.elb.amazonaws.com",
                  "https://container-service-1.abcd.us-east-1.cs.amazonlightsail.com",
                  "https://d123.cloudfront.net",
                  "https://myenv.eu-west-2.elasticbeanstalk.com",
                  "https://abcd1234.execute-api.us-east-1.amazonaws.com"):
            self.assertEqual(self._cands(f"deployed at {u} now"), [u], u)

    def test_service_and_api_endpoints_are_never_picked(self):
        for u in ("https://ec2.us-east-1.amazonaws.com",
                  "https://sts.amazonaws.com",
                  "https://s3.us-east-1.amazonaws.com",
                  "https://mybucket.s3.us-east-1.amazonaws.com",
                  "https://776638915601.dkr.ecr.us-east-1.amazonaws.com",
                  "https://aws-mcp.us-east-1.api.aws/mcp",
                  "https://console.aws.amazon.com/apprunner",
                  "https://docs.aws.amazon.com/apprunner/latest"):
            self.assertEqual(self._cands(f"the endpoint is {u}"), [], u)

    def test_app_front_wins_over_service_endpoints_in_same_text(self):
        text = ("built image at 776638915601.dkr.ecr.us-east-1.amazonaws.com/it-tools, "
                "called ec2.us-east-1.amazonaws.com, service live at "
                "https://p7q9.us-east-1.awsapprunner.com")
        self.assertEqual(self._cands(text), ["https://p7q9.us-east-1.awsapprunner.com"])


class TestTranscriptDir(unittest.TestCase):
    def test_slug_is_cwd_with_dashes(self):
        # measured 2026-08-25: claude -p in /tmp/acspeed-work-isso wrote ~/.claude/projects/-tmp-acspeed-work-isso/
        self.assertTrue(autorun.transcript_dir_for("/tmp/acspeed-work-isso")
                        .endswith("/.claude/projects/-tmp-acspeed-work-isso"))


def _row(text):
    return json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                       "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


class TestScreenshot(unittest.TestCase):
    """Optional, off-clock screenshot: best-effort, never raises, degrades to a skip."""

    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.path = os.path.join(self.out, "run01.png")

    def tearDown(self):
        shutil.rmtree(self.out, ignore_errors=True)

    def test_skips_cleanly_when_no_tool_available(self):
        with mock.patch.object(autorun, "_screenshot_playwright", return_value=False), \
             mock.patch.object(autorun, "shutil") as sh:
            sh.which.return_value = None
            r = autorun.capture_screenshot("https://x.redu.cloud", self.path)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["path"])
        self.assertIn("hint", r)

    def test_chrome_cli_success_reports_tool(self):
        def fake_run(cmd, **kw):
            open(cmd[-2].split("=", 1)[1], "wb").write(b"\x89PNG fake")   # --screenshot=<path>
            return None
        with mock.patch.object(autorun, "_screenshot_playwright", return_value=False), \
             mock.patch.object(autorun.shutil, "which", side_effect=lambda b: "/usr/bin/chromium" if b == "chromium" else None), \
             mock.patch.object(autorun.subprocess, "run", side_effect=fake_run):
            r = autorun.capture_screenshot("https://app.redu.cloud", self.path)
        self.assertTrue(r["ok"])
        self.assertEqual(r["tool"], "chromium")
        self.assertTrue(os.path.exists(self.path))

    def test_never_raises_on_internal_error(self):
        with mock.patch.object(autorun, "_screenshot_playwright", side_effect=RuntimeError("boom")):
            r = autorun.capture_screenshot("https://app.redu.cloud", self.path)
        self.assertFalse(r["ok"])
        self.assertIn("err", r)

    def test_playwright_used_when_it_succeeds(self):
        with mock.patch.object(autorun, "_screenshot_playwright", return_value=True):
            r = autorun.capture_screenshot("https://app.redu.cloud", self.path)
        self.assertTrue(r["ok"])
        self.assertEqual(r["tool"], "playwright")


class TestCurlDead(unittest.TestCase):
    """Teardown read-verify: 'dead' = the deployment is GONE (no orphan), which is NOT the liveness
    predicate. A deleted SERVERLESS service (Cloud Run / App Engine) answers 404 on its now-nonexistent
    route, which must count as torn down, not a false orphan on every clean serverless teardown."""

    def _dead(self, code):
        with mock.patch.object(autorun.subprocess, "run", return_value=mock.MagicMock(stdout=code)):
            return autorun.curl_dead("https://it-tools-1234.europe-west1.run.app")["dead"]

    def test_connection_failure_is_dead(self):
        self.assertTrue(self._dead("000"))

    def test_5xx_is_dead(self):
        self.assertTrue(self._dead("503"))

    def test_404_deleted_serverless_service_is_dead(self):   # the GCP Cloud Run false-orphan fix
        self.assertTrue(self._dead("404"))

    def test_403_access_gated_is_not_dead_possible_orphan(self):
        self.assertFalse(self._dead("403"))

    def test_200_still_serving_is_not_dead_real_orphan(self):
        self.assertFalse(self._dead("200"))


class TestSuiteTeardownOrphan(unittest.TestCase):
    """deprovision_suite_agent must use the teardown predicate (curl_dead), not the readiness predicate,
    when deciding whether a survivor remains. This is the end-to-end lock on the gcp Cloud Run false
    orphan: a clean serverless teardown answers 404 on the deleted routes and must NOT be flagged."""

    def _run(self, dead_map):
        # dead_map: url -> dead? ; one agent turn, then the loop polls curl_dead.
        with mock.patch.object(autorun, "_claude",
                               return_value={"session_id": "s2", "total_cost_usd": 0.0, "is_error": False}), \
             mock.patch.object(autorun, "find_transcript", return_value=None), \
             mock.patch.object(autorun.time, "sleep", lambda _s: None), \
             mock.patch.object(autorun, "curl_dead",
                               side_effect=lambda u: {"url": u, "http_code": "404", "dead": dead_map[u]}):
            return autorun.deprovision_suite_agent(
                {"mcp_config": None, "cloud": "gcp"}, None,
                "https://umami-acs123-9988.us-central1.run.app", "s1", None, tempfile.mkdtemp(), 1,
                tempfile.mkdtemp(), teardown_hint="umami + its db + the second site",
                verify_urls=["https://acspeed-site-abc-9988.us-central1.run.app"])

    def test_clean_serverless_teardown_is_not_orphaned(self):
        # both the primary and the second site return 404 after delete = torn down.
        td = self._run({"https://umami-acs123-9988.us-central1.run.app": True,
                        "https://acspeed-site-abc-9988.us-central1.run.app": True})
        self.assertFalse(td["orphaned"])
        self.assertEqual(td["live_urls"], [])

    def test_real_survivor_is_orphaned(self):
        # the second site is still serving (not dead) -> a genuine orphan must be flagged.
        surv = "https://acspeed-site-abc-9988.us-central1.run.app"
        td = self._run({"https://umami-acs123-9988.us-central1.run.app": True, surv: False})
        self.assertTrue(td["orphaned"])
        self.assertIn(surv, td["live_urls"])


class _FakeRedu:
    """Stands in for redu_mcp_http. list_* return the CURRENT rows (a successful delete removes the row, so
    a re-verify sees it gone). fail_ids: the delete raises AND the row stays (a genuine failure). timeout_ids:
    the row IS removed (the delete landed) but the call still raises TimeoutError (the response timed out) -
    the reaper's re-verify must count this reaped, not a false orphan."""
    def __init__(self, dbs, deps, fail_ids=(), timeout_ids=()):
        self.dbs, self.deps = list(dbs), list(deps)
        self.deleted, self.fail_ids, self.timeout_ids = [], set(fail_ids), set(timeout_ids)

    def call_tool(self, name, args):
        if name == "list_databases":
            return {"databases": self.dbs}
        if name == "list_deployments":
            return {"deployments": self.deps}
        if name == "delete_database":
            rid = args["id"]
            if rid in self.fail_ids:
                raise RuntimeError("delete refused")           # genuinely not deleted (row stays)
            self.dbs = [x for x in self.dbs if str(x.get("id")) != str(rid)]
            self.deleted.append(("db", rid))
            if rid in self.timeout_ids:
                raise TimeoutError("read timed out")           # deleted, but the response timed out
            return {"deleted": True}
        if name == "delete_deployment":
            rid = args["id"]
            self.deps = [x for x in self.deps if int(x.get("id")) != int(rid)]
            self.deleted.append(("dep", rid))
            return {"deleted": True}
        return {}


class TestReapRun(unittest.TestCase):
    """The last-line token-scoped reaper: it removes the no-URL resources the agent teardown leaves (a
    managed DB on redu, the whole resource group on azure) and NOTHING that lacks this run's token."""

    def test_redu_reaps_token_db_and_deployment_only(self):
        redu = _FakeRedu(
            dbs=[{"id": "250", "name": "umami-acsb44fe2e7"}, {"id": "9", "name": "other-app-db"}],
            deps=[{"id": 665, "name": "umami-acsb44fe2e7", "dname": "acsb44fe2e7-umami001.redu.cloud"},
                  {"id": 10, "name": "someone-elses-app", "dname": "x.redu.cloud"}])
        r = autorun.reap_run("redu", "acsb44fe2e7", "https://acsb44fe2e7-umami001.redu.cloud",
                             log=lambda *_a: None, redu=redu)
        self.assertEqual(r["failed"], [])
        self.assertEqual(len(r["reaped"]), 2)
        self.assertEqual(set(redu.deleted), {("db", "250"), ("dep", 665)})  # the other two were left alone

    def test_redu_reaps_second_site_by_url_host_without_token(self):
        # the sweep's bug: site B is named with the probe suffix, NOT the run token -> a token-only match
        # missed it and left a live billing VM. It must be caught by its known URL host.
        redu = _FakeRedu(dbs=[], deps=[
            {"id": 668, "name": "probe-site-b", "dname": "probe-site-b-a6068151.redu.cloud",
             "access_point": "https://probe-site-b-a6068151.redu.cloud"},
            {"id": 700, "name": "unrelated", "dname": "unrelated.redu.cloud",
             "access_point": "https://unrelated.redu.cloud"}])
        r = autorun.reap_run("redu", "acs91f5efff",
                             ["https://umami-acs91f5efff-7q3m2x8k.redu.cloud",
                              "https://probe-site-b-a6068151.redu.cloud"], log=lambda *_a: None, redu=redu)
        self.assertEqual(r["failed"], [])
        self.assertEqual(redu.deleted, [("dep", 668)])   # only the known site-B host; not the unrelated one

    def test_redu_timeout_but_actually_gone_is_reaped_not_failed(self):
        # the sweep's other bug: delete_database returned a read TIMEOUT yet the DB was actually deleted;
        # the reaper must re-verify and count it reaped, not raise a false orphan.
        redu = _FakeRedu(dbs=[{"id": "251", "name": "umami-acs91f5efff"}], deps=[], timeout_ids={"251"})
        r = autorun.reap_run("redu", "acs91f5efff", "https://umami-acs91f5efff.redu.cloud",
                             log=lambda *_a: None, redu=redu)
        self.assertEqual(r["failed"], [])
        self.assertEqual(len(r["reaped"]), 1)
        self.assertIn("confirmed gone", r["reaped"][0])

    def test_redu_failed_delete_is_flagged_as_orphan(self):
        redu = _FakeRedu(dbs=[{"id": "250", "name": "umami-acsb44fe2e7"}], deps=[], fail_ids={"250"})
        r = autorun.reap_run("redu", "acsb44fe2e7", "https://x-acsb44fe2e7.redu.cloud",
                             log=lambda *_a: None, redu=redu)
        self.assertTrue(r["failed"])          # a delete that raised AND left the row is a REAL orphan
        self.assertEqual(r["reaped"], [])

    def test_azure_reaps_run_resource_groups_universal(self):
        # azure now delegates to the universal reaper: match ANY group carrying the token (rg-umami-<token>
        # AND rg-<token>), collapse the run's resources to their groups, delete the groups (async --no-wait),
        # leave an unrelated group alone, and do NOT re-report an async-deleting group as a survivor.
        import json as _json
        deleted = []

        def sh(args, timeout=180):
            a = " ".join(args)
            if "resource list" in a:
                return (0, _json.dumps([{"name": "umami-acs1a2b3c4d",
                                         "resourceGroup": "rg-umami-acs1a2b3c4d"}]), "")
            if "group list" in a:
                return (0, _json.dumps(["rg-umami-acs1a2b3c4d", "rg-acs1a2b3c4d", "rg-someone-else"]), "")
            if "group delete" in a:
                deleted.append(args[args.index("-n") + 1])
                return (0, "", "")   # --no-wait accepts async; group may still list until gone
            return (0, "[]", "")

        r = autorun.reap_run("azure", "acs1a2b3c4d",
                             "https://umami-acs1a2b3c4d.env.westeurope.azurecontainerapps.io",
                             log=lambda *_a: None, sh=sh)
        self.assertEqual(r["failed"], [])                                           # no false survivors
        self.assertEqual(set(deleted), {"rg-umami-acs1a2b3c4d", "rg-acs1a2b3c4d"})  # both token-named groups
        self.assertNotIn("rg-someone-else", deleted)                                # unrelated group untouched
        self.assertEqual(len(r["reaped"]), 2)

    def test_no_token_is_a_noop(self):
        r = autorun.reap_run("redu", "", "url", log=lambda *_a: None)
        self.assertFalse(r["checked"])
        self.assertEqual(r["reaped"], [])


class TestReadinessPoller(unittest.TestCase):
    """The poller tails the live transcript for URL candidates, polls them, and fixes t1 on the
    first serving response, while the (simulated) agent session is still running."""

    URL_RE = r"https://[a-z0-9.-]+\.redu\.cloud"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.t0 = time.monotonic()
        self.t0_epoch = time.time()
        # C24: the poller fingerprints each serving poll and origin-probes each 4xx with real curls;
        # keep the tests offline and deterministic.
        self._fp = mock.patch.object(autorun, "fingerprint_response",
                                     return_value={"bytes": 9673, "sig": "app"})
        self._po = mock.patch.object(autorun, "probe_origin", return_value={"edge": None})
        self._fp.start()
        self._po.start()
        self._pollers: list = []

    def tearDown(self):
        for p in self._pollers:                 # C24: the poller no longer self-terminates on serve
            p.stop()
            p.join(timeout=3)
        self._po.stop()
        self._fp.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _poller(self, **kw):
        p = autorun.ReadinessPoller(self.t0, self.t0_epoch, self.dir, self.URL_RE,
                                    interval_s=kw.pop("interval_s", 0.05), **kw)
        self._pollers.append(p)      # tearDown stops every poller (they no longer self-terminate: C24)
        return p

    def test_t1_is_caught_while_the_agent_is_still_working(self):
        codes = iter(["000", "502", "400"])          # boot: nothing, gateway, then the app answers 400
        with mock.patch.object(autorun, "is_serving",
                               side_effect=lambda u: (c := next(codes, "400"), autorun.serving_predicate(c))):
            p = self._poller()
            p.start()
            time.sleep(0.12)                          # no transcript yet: nothing to poll
            self.assertIsNone(p.t_url_seen_s)
            with open(os.path.join(self.dir, "s1.jsonl"), "w") as fh:
                fh.write(_row("Deployment created: https://isso-abc123.redu.cloud is booting"))
            time.sleep(0.3)                           # C24: the poller keeps polling past the first serve
            p.stop()                                  # the caller halts it at the end of the deploy turn
            p.join(timeout=3)
            self.assertFalse(p.is_alive())
            self.assertEqual(p.served_url, "https://isso-abc123.redu.cloud")
            self.assertEqual(p.code, "400")           # Isso's documented 400 counts as serving (first serve)
            self.assertIsNotNone(p.t_serving_s)
            self.assertGreaterEqual(p.t_serving_s, p.t_url_seen_s)
            self.assertGreaterEqual(p.polls, 3)       # at least the 3 boot polls; more after (keeps polling)
            self.assertFalse(p.served_on_first_poll)
            self.assertTrue(p.poll_log and p.poll_log[0]["code"] == "400")   # serving polls are logged
            self.assertAlmostEqual(p.serving_epoch, self.t0_epoch + p.t_serving_s, delta=0.5)

    def test_stale_hostname_from_a_config_file_is_polled_but_harmless(self):
        # the agent cats a config carrying last deploy's hostname (000 forever) before the new one appears
        def fake(u):
            return ("200", True) if "new-host" in u else ("000", False)
        with mock.patch.object(autorun, "is_serving", side_effect=fake):
            with open(os.path.join(self.dir, "s1.jsonl"), "w") as fh:
                fh.write(_row("public-endpoint = https://old-host.redu.cloud"))
            p = self._poller()
            p.start()
            time.sleep(0.15)
            self.assertIsNone(p.served_url)
            with open(os.path.join(self.dir, "s1.jsonl"), "a") as fh:
                fh.write(_row("Deployed at https://new-host.redu.cloud"))
            p.join(timeout=3)
            self.assertEqual(p.served_url, "https://new-host.redu.cloud")
            self.assertEqual(p.candidates, ["https://old-host.redu.cloud", "https://new-host.redu.cloud"])

    def test_first_poll_serving_is_flagged(self):
        with mock.patch.object(autorun, "is_serving", return_value=("200", True)):
            with open(os.path.join(self.dir, "s1.jsonl"), "w") as fh:
                fh.write(_row("https://leftover.redu.cloud"))
            p = self._poller()
            p.start()
            p.join(timeout=3)
            self.assertTrue(p.served_on_first_poll)

    def test_aux_hosts_are_not_candidates(self):
        with mock.patch.object(autorun, "is_serving", return_value=("000", False)):
            with open(os.path.join(self.dir, "s1.jsonl"), "w") as fh:
                fh.write(_row("https://jaeger-x.redu.cloud and https://frontend-x.redu.cloud"))
            p = self._poller()
            p.start()
            time.sleep(0.15)
            p.stop()
            p.join(timeout=3)
            self.assertEqual(p.candidates, ["https://frontend-x.redu.cloud"])

    def test_stop_ends_the_thread_without_serving(self):
        with mock.patch.object(autorun, "is_serving", return_value=("502", False)):
            with open(os.path.join(self.dir, "s1.jsonl"), "w") as fh:
                fh.write(_row("https://app-x.redu.cloud"))
            p = self._poller()
            p.start()
            time.sleep(0.15)
            p.stop()
            p.join(timeout=3)
            self.assertFalse(p.is_alive())
            self.assertIsNone(p.t_serving_s)
            self.assertEqual(p.last_codes, {"https://app-x.redu.cloud": "502"})

    def test_old_transcripts_in_the_dir_are_ignored(self):
        stale = os.path.join(self.dir, "old.jsonl")
        with open(stale, "w") as fh:
            fh.write(_row("https://stale.redu.cloud"))
        os.utime(stale, (self.t0_epoch - 600, self.t0_epoch - 600))   # written long before t0
        with mock.patch.object(autorun, "is_serving", return_value=("200", True)):
            p = self._poller()
            p.start()
            time.sleep(0.15)
            p.stop()
            p.join(timeout=3)
            self.assertEqual(p.candidates, [])


class TestClaudeAuthPreflight(unittest.TestCase):
    """The fail-fast gate that refuses to START on a dead Claude login. Root cause of the 2026-08-31
    4-cloud wipeout: every run failed 'OAuth session expired', nothing failed fast, so deploys burned and
    RDS/ECS/CloudSQL/an Azure RG were orphaned. Proves BOTH directions - a live round-trip passes, a dead
    one is refused with the actionable fix - so the check cannot silently pass on a broken login."""

    def _call(self, *, returncode=0, result="READY", is_error=False):
        payload = json.dumps({"type": "result", "is_error": is_error, "result": result})
        cp = mock.Mock(returncode=returncode, stdout=payload, stderr="")
        with mock.patch.object(autorun.subprocess, "run", return_value=cp):
            return autorun.preflight_claude_auth()

    def test_live_login_passes(self):
        ok, why = self._call(result="READY", is_error=False, returncode=0)
        self.assertTrue(ok)
        self.assertIn("OK", why)

    def test_expired_oauth_is_refused(self):
        ok, why = self._call(result="Failed to authenticate: OAuth session expired and could not be refreshed",
                             is_error=True, returncode=0)
        self.assertFalse(ok)
        self.assertIn("setup-token", why)   # the durable fix is surfaced, not just "it failed"

    def test_nonzero_exit_is_refused(self):
        ok, _ = self._call(returncode=1, result="", is_error=False)
        self.assertFalse(ok)

    def test_authenticate_in_result_is_caught_even_if_flags_look_clean(self):
        ok, _ = self._call(result="could not authenticate the request", is_error=False, returncode=0)
        self.assertFalse(ok)

    def test_low_ttl_passes_but_is_flagged(self):
        # a valid-but-nearly-expired token is still usable (passes) but LOUDLY warns the batch may outlive it
        creds = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r",
                                   "expiresAt": (time.time() + 30 * 60) * 1000}}   # 30 min TTL
        cp = mock.Mock(returncode=0, stdout=json.dumps({"is_error": False, "result": "READY"}), stderr="")
        with mock.patch.object(autorun.subprocess, "run", return_value=cp), \
             mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(creds))):
            ok, why = autorun.preflight_claude_auth()
        self.assertTrue(ok)
        self.assertIn("LOW", why)


@unittest.skipUnless(getattr(autorun, "vmjob", None) is not None,
                     "sandbox/vmjob.py not present (private microVM substrate; gitignored)")
class TestVmBootFlakeRetry(unittest.TestCase):
    """A microVM turn that yields NO agent result (empty out.json / session=None, exit 143) is an infra
    boot-flake and is retried in a fresh VM; a real result (even an error) is returned immediately and NEVER
    retried, so a genuine agent failure is not masked and a partial deploy is not double-provisioned. Root
    cause of the medium tier failing 2026-08-31 (deploy-site-b, no-retry op) even though auth was fine."""

    def _sandbox(self):
        return {"keep_claude_tokens": ["redu"], "aws_dir": None, "creds_mounts": {}, "session_store": None}

    def _call(self, side_effect):
        with mock.patch.object(autorun.vmjob, "run_vm_job", side_effect=side_effect) as rvj, \
             mock.patch.object(autorun, "_install_recovered_ssh_keys", lambda *_a, **_k: None):
            out = autorun._claude_vm("task", app_dir="/x", mcp=None, model="m", resume=None,
                                     max_turns=10, timeout=100, system=None, sandbox=self._sandbox(),
                                     boot_log=None, resume_transcript=None)
        return out, rvj

    def _flake(self):   # guest torn down before claude wrote a result; work_dir=None so no real cleanup runs
        return {"result": None, "exit_code": "143", "timed_out": False, "work_dir": None,
                "boot_log": None, "session_id": None}

    def _ok(self, sid="s1", is_error=False):
        return {"result": {"is_error": is_error, "session_id": sid, "result": "done"},
                "exit_code": "0", "timed_out": False, "work_dir": None, "boot_log": None, "session_id": sid}

    def test_retries_boot_flake_until_a_real_result(self):
        out, rvj = self._call([self._flake(), self._flake(), self._ok(sid="good")])
        self.assertEqual(out.get("session_id"), "good")
        self.assertEqual(rvj.call_count, 3)          # two flakes retried, third booted

    def test_real_result_first_is_not_retried(self):
        out, rvj = self._call([self._ok(sid="first")])
        self.assertEqual(out.get("session_id"), "first")
        self.assertEqual(rvj.call_count, 1)

    def test_agent_error_result_is_not_retried(self):
        # an agent that RAN and returned an error (has a session) is a real outcome, never a boot-flake
        out, rvj = self._call([self._ok(sid="errsess", is_error=True)])
        self.assertTrue(out.get("is_error"))
        self.assertEqual(out.get("session_id"), "errsess")
        self.assertEqual(rvj.call_count, 1)

    def test_exhausts_retries_then_reports_no_result(self):
        out, rvj = self._call([self._flake(), self._flake(), self._flake()])
        self.assertTrue(out.get("is_error"))
        self.assertIsNone(out.get("session_id"))
        self.assertIn("no result", out.get("result", ""))
        self.assertEqual(rvj.call_count, autorun.VM_BOOT_RETRIES + 1)   # bounded, does not loop forever


class TestEdge4xxOriginCheck(unittest.TestCase):
    """PAPER p2 s4: 'any adapter whose edge answers 4xx for an unwired host must check response origin.'

    MEASURED 2026-09-03: every AWS Lightsail run in the medium-b batch stopped the clock on the FIRST
    poll with a 404 (t1 160-250s) while the same workload over an ALB, whose t1 is a real app 200, took
    436-867s; the off-clock oracle later found the real app in each case. A GCP Cloud Run URL 403s from
    its IAM edge the same way. The predicate itself is unchanged (a 4xx is still the app answering by
    default); only a response positively identified as a platform edge is refused."""

    def test_alb_no_upstream_404_is_not_serving(self):
        with mock.patch.object(autorun, "is_serving", return_value=("404", True)), \
             mock.patch.object(autorun, "probe_origin",
                               return_value={"edge": "aws-alb-no-upstream", "server": "awselb/2.0",
                                             "body_bytes": 0, "body_sample": ""}):
            code, ok, origin = autorun.is_serving_ex("http://x.elb.amazonaws.com")
        self.assertEqual(code, "404")
        self.assertFalse(ok, "an ALB-generated 404 is the edge, not the app")
        self.assertEqual(origin["edge"], "aws-alb-no-upstream")

    def test_cloud_run_iam_403_is_not_serving(self):
        with mock.patch.object(autorun, "is_serving", return_value=("403", True)), \
             mock.patch.object(autorun, "probe_origin",
                               return_value={"edge": "gcp-iam-forbidden", "server": "Google Frontend",
                                             "body_bytes": 300, "body_sample": "forbidden"}):
            _, ok, origin = autorun.is_serving_ex("https://x.run.app")
        self.assertFalse(ok)
        self.assertEqual(origin["edge"], "gcp-iam-forbidden")

    def test_a_real_app_4xx_still_serves(self):
        """Isso answers 400 at '/', a SPA 404s, an API 401s: all are the app answering (unchanged)."""
        for code in ("400", "401", "404"):
            with mock.patch.object(autorun, "is_serving", return_value=(code, True)), \
                 mock.patch.object(autorun, "probe_origin",
                                   return_value={"edge": None, "server": "nginx",
                                                 "body_bytes": 120, "body_sample": "<!doctype html>"}):
                _, ok, origin = autorun.is_serving_ex("https://isso.example")
            self.assertTrue(ok, f"{code} from the app must still stop the clock")
            self.assertIsNone(origin["edge"])

    def test_2xx_never_pays_for_an_origin_probe(self):
        with mock.patch.object(autorun, "is_serving", return_value=("200", True)), \
             mock.patch.object(autorun, "probe_origin") as po:
            _, ok, origin = autorun.is_serving_ex("https://x.example")
        self.assertTrue(ok)
        self.assertIsNone(origin)
        po.assert_not_called()

    def test_a_failed_origin_probe_never_fakes_a_t1(self):
        """If the evidence cannot be gathered, the 4xx keeps its original verdict, disclosed."""
        with mock.patch.object(autorun, "is_serving", return_value=("404", True)), \
             mock.patch.object(autorun, "probe_origin", side_effect=RuntimeError("network")):
            _, ok, origin = autorun.is_serving_ex("https://x.example")
        self.assertTrue(ok)
        self.assertEqual(origin, {"edge": None, "error": "origin probe failed"})

    def test_signatures_match_on_header_and_on_body(self):
        """probe_origin's own matching, over raw curl output (headers, blank line, body)."""
        alb = "HTTP/1.1 404 Not Found\r\nServer: awselb/2.0\r\nContent-Length: 0\r\n\r\n"
        gcp = ("HTTP/2 403\r\nserver: Google Frontend\r\n\r\n"
               "<html><title>403 Forbidden</title>Your client does not have permission</html>")
        app = "HTTP/1.1 400 Bad Request\r\nServer: nginx\r\n\r\n<html>missing ?uri= parameter</html>"
        for raw, expect in ((alb, "aws-alb-no-upstream"), (gcp, "gcp-iam-forbidden"), (app, None)):
            with mock.patch.object(autorun.subprocess, "run",
                                   return_value=mock.Mock(stdout=raw, returncode=0)):
                self.assertEqual(autorun.probe_origin("https://x.example")["edge"], expect)

    def test_poller_records_every_4xx_verdict(self):
        """The rejections are kept too, so the decision is auditable from the run json."""
        p = autorun.ReadinessPoller(time.monotonic(), time.time(), tempfile.mkdtemp(),
                                    r"https://[a-z0-9.-]+\.example", interval_s=0.05)
        codes = iter([("404", False, {"edge": "aws-alb-no-upstream", "server": "awselb/2.0"}),
                      ("200", True, None)])
        with mock.patch.object(autorun, "is_serving_ex",
                               side_effect=lambda u: next(codes, ("200", True, None))), \
             mock.patch.object(autorun, "fingerprint_response", return_value={"bytes": 9673, "sig": "app"}):
            p.candidates.append("https://app.example")
            p.start()
            time.sleep(0.3)               # C24: the poller keeps polling; let it see the 404 then the 200
            p.stop()
            p.join(timeout=3)
        self.assertEqual(p.rejected_edge_4xx, 1)
        self.assertEqual(len(p.origin_checks), 1)
        self.assertEqual(p.origin_checks[0]["edge"], "aws-alb-no-upstream")
        self.assertEqual(p.code, "200", "the clock stops on the app's 200, not the edge 404")


class DurableServing(unittest.TestCase):
    """PAPER C24: t1 is the earliest poll matching the app's stable FINAL response, so a transient edge
    placeholder is superseded and a stable app response (even a 4xx root) stops the clock at its first
    poll."""

    def test_clean_200_run_t1_is_the_first_poll(self):
        # the first serve WAS the app: durable t1 == first-serve t1 (the 74/78 already-published case)
        log = [{"t_s": 300.0, "url": "u", "code": "200", "bytes": 9673, "sig": "a"},
               {"t_s": 305.0, "url": "u", "code": "200", "bytes": 9673, "sig": "a"}]
        self.assertEqual(autorun.durable_serving(log), (300.0, "200"))

    def test_edge_404_blip_then_app_200_t1_is_the_200(self):
        # Lightsail-style: a 404 edge stopped the first-serve clock at 160s; the real app 200 came at 900s
        log = [{"t_s": 160.0, "url": "u", "code": "404", "bytes": 250, "sig": "e"},
               {"t_s": 900.0, "url": "u", "code": "200", "bytes": 9673, "sig": "a"},
               {"t_s": 905.0, "url": "u", "code": "200", "bytes": 9673, "sig": "a"}]
        self.assertEqual(autorun.durable_serving(log), (900.0, "200"),
                         "the transient edge 404 must be skipped; t1 is the first real-app 200")

    def test_stable_app_4xx_root_keeps_its_first_poll(self):
        # Isso answers 400 at '/' forever: it matches its own final response, so t1 is its first poll
        log = [{"t_s": 120.0, "url": "u", "code": "400", "bytes": 512, "sig": "i"},
               {"t_s": 125.0, "url": "u", "code": "400", "bytes": 512, "sig": "i"}]
        self.assertEqual(autorun.durable_serving(log), (120.0, "400"))

    def test_dynamic_body_size_within_tolerance_still_matches(self):
        # a CSRF token moves the body a few bytes between polls; same code + near-size must still match
        log = [{"t_s": 200.0, "url": "u", "code": "200", "bytes": 9670, "sig": "a"},
               {"t_s": 400.0, "url": "u", "code": "200", "bytes": 9673, "sig": "b"}]
        self.assertEqual(autorun.durable_serving(log), (200.0, "200"))

    def test_empty_log_returns_none(self):
        self.assertEqual(autorun.durable_serving([]), (None, None))


if __name__ == "__main__":
    unittest.main()
