"""Tests for the umami Medium INSTANCE verify predicates (acspeed/suites/umami_medium.py).

The design contract says a verify predicate that cannot fail is not a control. So for every one of the
seven boundary reads this proves BOTH directions: it passes when the postcondition holds AND it fails
when the postcondition is violated. HTTP (umami / site B) and the headless-browser visit are mocked, so
there is no live app and no network; the mocks are the only place a "true" can come from, and each test
withholds it to show the read goes false.
"""
import unittest
from unittest import mock

from acspeed.suites import umami_medium as um
from acspeed.suite import OpContext


UMAMI = "https://umami.example"
SITEB = "https://siteb.example"
PROBE = {
    "username": "acspeed_probe_dead", "password": "pw",
    "website_name": "acspeed-site-dead", "sentinel_path": "/acspeed-sentinel-dead", "suffix": "dead",
}


def _ctx(**state):
    c = OpContext(url=UMAMI)
    c.state["probe"] = PROBE
    c.state["umami_url"] = UMAMI
    c.state.update(state)
    return c


def _canonical_siteb():
    """Site B as first deployed (what R3 reads): the standard page, NOT yet wired to umami."""
    return um._canonical_site_b_html()


def _wired_siteb(website_id="w1", umami=UMAMI):
    """Site B after the agent wires umami in during the integrate step (what R4/R5 read)."""
    snippet = f'<script defer src="{umami}/script.js" data-website-id="{website_id}"></script>'
    return _canonical_siteb().replace("</head>", snippet + "\n</head>")


class Router:
    """A stateful fake for ``umami_medium.http``: routes (method, url) -> (code, text, parsed). A
    ``visited`` flag flips the site-B pageview count from 0 to 1, so only a headless visit can make the
    integration read pass. The per-URL metrics also carry UNRELATED root traffic (``root_pv``, default 7),
    which the sentinel-path-scoped read must IGNORE - that isolation is the contamination fix."""

    def __init__(self, *, umami_serves=True, login_ok=True, siteb_html=None,
                 websites=None, pv_after_visit=1, root_pv=7):
        self.umami_serves = umami_serves
        self.login_ok = login_ok
        self.siteb_html = siteb_html if siteb_html is not None else _wired_siteb()
        self.websites = websites if websites is not None else [
            {"id": "w1", "name": PROBE["website_name"], "domain": "siteb.example"}]
        self.pv_after_visit = pv_after_visit
        self.root_pv = root_pv
        self.visited = False

    def __call__(self, method, url, *, body=None, bearer=None, headers=None, timeout_s=20):
        m, u = method.upper(), url
        if u.rstrip("/") == UMAMI and m == "GET":
            return (200, "<html>umami login</html>", None) if self.umami_serves else (None, "", None)
        if u.endswith("/api/auth/login") and m == "POST":
            return (200, "", {"token": "tok"}) if self.login_ok else (401, "", None)
        if u.endswith("/api/websites") and m == "GET":
            return 200, "", self.websites
        if "/metrics" in u and m == "GET":
            pv = self.pv_after_visit if self.visited else 0
            # per-URL breakdown: our sentinel path (what R5/R7 must count) + unrelated root traffic.
            return 200, "", [{"x": PROBE["sentinel_path"], "y": pv}, {"x": "/", "y": self.root_pv}]
        if "/stats" in u and m == "GET":                        # aggregate (legacy / Hard tier)
            pv = self.pv_after_visit if self.visited else 0
            return 200, "", {"pageviews": {"value": pv + self.root_pv}}
        if u.rstrip("/") == SITEB or u.startswith(SITEB):
            return 200, self.siteb_html, None
        return None, "", None


def _visit_ok(url, settle_s=8.0):
    return True, "playwright"


class ReadR1(unittest.TestCase):
    def test_pass_when_umami_serves_with_signature(self):
        with mock.patch.object(um, "http", Router(umami_serves=True)):
            self.assertTrue(um._r1_umami_serves(_ctx()).ok)

    def test_fail_when_root_dead(self):
        with mock.patch.object(um, "http", Router(umami_serves=False)):
            self.assertFalse(um._r1_umami_serves(_ctx()).ok)

    def test_fail_when_no_umami_signature(self):
        r = Router()
        r.umami_serves = True
        with mock.patch.object(um, "http", lambda *a, **k: (200, "<html>nginx</html>", None)):
            self.assertFalse(um._r1_umami_serves(_ctx()).ok)


class ReadR2(unittest.TestCase):
    def test_pass_when_user_authenticates(self):
        with mock.patch.object(um, "http", Router(login_ok=True)):
            self.assertTrue(um._r2_user_exists(_ctx()).ok)

    def test_fail_when_login_rejected(self):
        with mock.patch.object(um, "http", Router(login_ok=False)):
            self.assertFalse(um._r2_user_exists(_ctx()).ok)


class ReadR3(unittest.TestCase):
    def test_pass_when_site_b_serves_canonical_page(self):
        # R3 reads the page as first deployed (un-wired standard page): the canonical title is enough.
        with mock.patch.object(um, "http", Router(siteb_html=_canonical_siteb())):
            self.assertTrue(um._r3_site_b_serves(_ctx(site_b_url=SITEB)).ok)

    def test_fail_when_no_site_b_url(self):
        with mock.patch.object(um, "http", Router()):
            self.assertFalse(um._r3_site_b_serves(_ctx()).ok)

    def test_fail_when_page_is_not_canonical(self):
        with mock.patch.object(um, "http", Router(siteb_html="<html>some other page</html>")):
            self.assertFalse(um._r3_site_b_serves(_ctx(site_b_url=SITEB)).ok)


class ReadR4Wiring(unittest.TestCase):
    def test_wiring_ok_pure(self):
        html = _wired_siteb(website_id="abc123", umami=UMAMI)
        ok, wid = um._wiring_ok(html, "umami.example")
        self.assertTrue(ok)
        self.assertEqual(wid, "abc123")

    def test_as_deployed_page_is_not_yet_wired(self):
        # the page deploy-site-b serves has NO umami snippet (the integration is not leaked into that
        # step); R4 must go false on it, and only pass once the agent wires umami in at integrate.
        ok, _ = um._wiring_ok(_canonical_siteb(), "umami.example")
        self.assertFalse(ok)

    def test_wiring_missing_script(self):
        ok, wid = um._wiring_ok("<html><head></head><body>no script</body></html>", "umami.example")
        self.assertFalse(ok)

    def test_wiring_points_at_wrong_umami(self):
        html = _wired_siteb(website_id="abc", umami="https://someone-else.example")
        ok, _ = um._wiring_ok(html, "umami.example")
        self.assertFalse(ok)  # snippet exists but not this run's umami

    def test_wiring_blank_website_id(self):
        html = _wired_siteb(website_id="", umami=UMAMI)
        ok, _ = um._wiring_ok(html, "umami.example")
        self.assertFalse(ok)

    def test_r4_pass_and_fail(self):
        with mock.patch.object(um, "http", Router(siteb_html=_wired_siteb("w1"))):
            self.assertTrue(um._r4_wiring_present(_ctx(site_b_url=SITEB)).ok)
        with mock.patch.object(um, "http", Router(siteb_html="<html>unwired</html>")):
            self.assertFalse(um._r4_wiring_present(_ctx(site_b_url=SITEB)).ok)


class ReadR5Visit(unittest.TestCase):
    def _run(self, router, visit=_visit_ok):
        with mock.patch.object(um, "http", router), \
             mock.patch.object(um, "headless_visit", visit), \
             mock.patch.object(um, "_INGEST_POLL_MAX_S", 0.0), \
             mock.patch.object(um.time, "sleep", lambda _s: None):
            return um._r5_visit_recorded(_ctx(site_b_url=SITEB))

    def test_pass_when_visit_registers_a_pageview(self):
        r = Router(pv_after_visit=1)

        def visit(url, settle_s=8.0):
            r.visited = True  # the harness visit is what creates the pageview
            return True, "playwright"

        self.assertTrue(self._run(r, visit).ok)

    def test_fail_when_visit_does_not_register(self):
        # the browser visited, but umami never counted it (broken wiring / dead ingest): pv stays 0.
        r = Router(pv_after_visit=1)
        self.assertFalse(self._run(r, lambda u, settle_s=8.0: (True, "playwright")).ok)

    def test_fail_when_no_browser_available(self):
        r = Router()
        res = self._run(r, lambda u, settle_s=8.0: (False, "none"))
        self.assertFalse(res.ok)
        self.assertIn("no headless browser", res.detail)

    def test_fail_when_no_website_registered(self):
        r = Router(websites=[])
        self.assertFalse(self._run(r).ok)

    def test_fail_when_cannot_authenticate(self):
        r = Router(login_ok=False)
        self.assertFalse(self._run(r).ok)

    def test_sentinel_path_isolates_from_unrelated_traffic(self):
        # heavy unrelated root traffic (root_pv=99) must NOT move the sentinel-path count: R5 reads a clean
        # 0 -> 1 from its single visit, immune to the agent's own test pages / bots / the site root.
        r = Router(pv_after_visit=1, root_pv=99)

        def visit(url, settle_s=8.0):
            r.visited = True
            return True, "playwright"

        res = self._run(r, visit)
        self.assertTrue(res.ok)
        self.assertEqual(res.measured["before"], 0)   # not 99: root traffic is excluded
        self.assertEqual(res.measured["after"], 1)
        self.assertEqual(res.measured["path"], PROBE["sentinel_path"])

    def test_uses_wired_website_id_over_fuzzy_lookup(self):
        # when R4 has wired an exact website id into the page, R5 must read THAT id (the one the beacon
        # fires with), not re-pick among the umami websites the agent created.
        r = Router(pv_after_visit=1)

        def visit(url, settle_s=8.0):
            r.visited = True
            return True, "playwright"

        with mock.patch.object(um, "http", r), \
             mock.patch.object(um, "headless_visit", visit), \
             mock.patch.object(um, "_INGEST_POLL_MAX_S", 0.0), \
             mock.patch.object(um.time, "sleep", lambda _s: None):
            res = um._r5_visit_recorded(_ctx(site_b_url=SITEB, site_b_website_id="wired-exact-99"))
        self.assertTrue(res.ok)
        self.assertEqual(res.measured["website_id"], "wired-exact-99")

    def test_ingest_poll_recovers_a_late_landing_pageview(self):
        # the aws CloudFront case: the pageview does NOT land in the first read after the visit but does a
        # few reads later. The ingest poll must recover it; a single fixed-sleep read would have failed the op.
        r = Router(pv_after_visit=1)
        reads = {"n": 0}
        real_count = um._pageview_count

        def visit(url, settle_s=8.0):
            r.visited = True
            return True, "playwright"

        def delayed_count(ctx, tok, wid, path=None):
            reads["n"] += 1
            if reads["n"] <= 3:     # baseline + first 2 post-visit reads still show 0 (CDN/ingest lag)
                return 0
            return real_count(ctx, tok, wid, path=path)   # then it lands

        with mock.patch.object(um, "http", r), \
             mock.patch.object(um, "headless_visit", visit), \
             mock.patch.object(um, "_pageview_count", delayed_count), \
             mock.patch.object(um, "_INGEST_POLL_MAX_S", 30.0), \
             mock.patch.object(um, "_INGEST_POLL_INTERVAL_S", 0.001), \
             mock.patch.object(um.time, "sleep", lambda _s: None):
            res = um._r5_visit_recorded(_ctx(site_b_url=SITEB))
        self.assertTrue(res.ok)                 # recovered
        self.assertEqual(res.measured["after"], 1)
        self.assertGreaterEqual(reads["n"], 4)  # it actually polled past the early zeros


class ReadR7Persistence(unittest.TestCase):
    def test_r5_fails_when_baseline_unreadable(self):
        # a flaky baseline (the per-URL read returns an unrecognized shape) must be unconfirmed, not
        # coerced to 0 (which could false-pass on the terminal re-read).
        class NoBaseline(Router):
            def __call__(self, method, url, **k):
                if "/metrics" in url:
                    return 200, "", {"unexpected": "shape"}   # not a list -> path read is unconfirmed
                return super().__call__(method, url, **k)
        with mock.patch.object(um, "http", NoBaseline()), \
             mock.patch.object(um, "headless_visit", _visit_ok), \
             mock.patch.object(um.time, "sleep", lambda _s: None):
            res = um._r5_visit_recorded(_ctx(site_b_url=SITEB))
        self.assertFalse(res.ok)
        self.assertIn("unrecognized shape", res.detail)

    def test_r7_pass_when_count_holds_at_floor(self):
        r = Router(pv_after_visit=3); r.visited = True  # sentinel path already holds 3 pageviews
        with mock.patch.object(um, "http", r):
            self.assertTrue(um._r7_pageview_persists(_ctx(
                site_b_url=SITEB, site_b_website_id="w1", pageview_floor=3,
                pageview_path=PROBE["sentinel_path"])).ok)

    def test_r7_fails_when_events_lost(self):
        # the sentinel-path count dropped back to 0 after a restart, EVEN THOUGH unrelated root traffic
        # (root_pv=50) is still counted website-wide - the path scoping is what exposes the real loss.
        r = Router(pv_after_visit=1, root_pv=50); r.visited = False
        with mock.patch.object(um, "http", r):
            res = um._r7_pageview_persists(_ctx(
                site_b_url=SITEB, site_b_website_id="w1", pageview_floor=1,
                pageview_path=PROBE["sentinel_path"]))
        self.assertFalse(res.ok)
        self.assertIn("LOST", res.detail)

    def test_integrate_terminal_uses_persistence_not_a_new_visit(self):
        # once the floor is recorded (mid-run visit happened), the integrate re-verify must check
        # persistence and NOT drive another visit.
        inst = um.build(probe=PROBE)
        integrate = next(o for o in inst.operations if o.op_id == "integrate")
        visits = {"n": 0}

        def counting_visit(url, settle_s=8.0):
            visits["n"] += 1
            return True, "playwright"

        r = Router(pv_after_visit=1); r.visited = True  # historical pageview present
        ctx = _ctx(site_b_url=SITEB, site_b_website_id="w1", pageview_floor=1,
                   pageview_path=PROBE["sentinel_path"])
        with mock.patch.object(um, "http", r), \
             mock.patch.object(um, "headless_visit", counting_visit), \
             mock.patch.object(um.time, "sleep", lambda _s: None):
            res = integrate.verify(ctx)
        self.assertTrue(res.ok)
        self.assertEqual(visits["n"], 0)  # persistence read, no new visit driven

    def test_reestablish_redrives_visit_when_integrate_lost(self):
        r = Router(pv_after_visit=1)
        ctx = _ctx(site_b_url=SITEB, site_b_website_id="w1")

        def visit(url, settle_s=8.0):
            r.visited = True
            return True, "playwright"

        with mock.patch.object(um, "http", r), \
             mock.patch.object(um, "headless_visit", visit), \
             mock.patch.object(um.time, "sleep", lambda _s: None):
            um._reestablish_visit(ctx, ["integrate"])
        self.assertEqual(ctx.state.get("pageview_floor"), 1)  # re-established a fresh pageview + floor


class IntegrateReadCombined(unittest.TestCase):
    def test_integrate_fails_fast_when_unwired(self):
        # v_integrate must not report a pass on the visit if the wiring read already failed.
        inst = um.build(probe=PROBE)
        integrate = next(o for o in inst.operations if o.op_id == "integrate")
        r = Router(siteb_html="<html>unwired</html>")
        with mock.patch.object(um, "http", r), \
             mock.patch.object(um, "headless_visit", _visit_ok), \
             mock.patch.object(um, "_WIRING_POLL_MAX_S", 0.0), \
             mock.patch.object(um.time, "sleep", lambda _s: None):
            res = integrate.verify(_ctx(site_b_url=SITEB))
        self.assertFalse(res.ok)


class WiringPropagationPoll(unittest.TestCase):
    """R4 is polled so an immutable host's redeploy propagation lag (Cloud Run revision migration) is not
    read as a real failure - measured on gcp: integrate failed 'missing wiring' yet the durability re-verify
    saw the same wiring present. The poll must NOT mask a genuinely un-wired page."""

    def test_recovers_when_wiring_lands_late(self):
        seq = [um.VerifyResult(False, "site B HTML missing umami wiring"),
               um.VerifyResult(True, "site B HTML carries umami wiring", measured={"website_id": "w1"})]
        with mock.patch.object(um, "_r4_wiring_present", side_effect=seq), \
             mock.patch.object(um.time, "sleep", lambda _s: None):
            r = um._wiring_present_polled(_ctx(site_b_url=SITEB), max_wait_s=30, interval_s=0.01)
        self.assertTrue(r.ok)

    def test_never_wired_still_fails_after_window(self):
        with mock.patch.object(um, "_r4_wiring_present",
                               return_value=um.VerifyResult(False, "site B HTML missing umami wiring")), \
             mock.patch.object(um.time, "sleep", lambda _s: None):
            r = um._wiring_present_polled(_ctx(site_b_url=SITEB), max_wait_s=0.0, interval_s=0.01)
        self.assertFalse(r.ok)
        self.assertIn("missing", r.detail)


if __name__ == "__main__":
    unittest.main()
