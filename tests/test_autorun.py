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


class TestReadinessPoller(unittest.TestCase):
    """The poller tails the live transcript for URL candidates, polls them, and fixes t1 on the
    first serving response, while the (simulated) agent session is still running."""

    URL_RE = r"https://[a-z0-9.-]+\.redu\.cloud"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.t0 = time.monotonic()
        self.t0_epoch = time.time()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _poller(self, **kw):
        return autorun.ReadinessPoller(self.t0, self.t0_epoch, self.dir, self.URL_RE,
                                       interval_s=kw.pop("interval_s", 0.05), **kw)

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
            p.join(timeout=3)                         # the agent 'keeps working'; the poller returns on its own
            self.assertFalse(p.is_alive())
            self.assertEqual(p.served_url, "https://isso-abc123.redu.cloud")
            self.assertEqual(p.code, "400")           # Isso's documented 400 counts as serving
            self.assertIsNotNone(p.t_serving_s)
            self.assertGreaterEqual(p.t_serving_s, p.t_url_seen_s)
            self.assertEqual(p.polls, 3)
            self.assertFalse(p.served_on_first_poll)
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


if __name__ == "__main__":
    unittest.main()
