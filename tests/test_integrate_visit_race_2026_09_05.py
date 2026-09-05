"""The integrate read scored the HARNESS's own blind spots as cloud failures.

Measured 2026-09-05 over 96 integrate checks in the results tree:

    engine          passed  failed  fail rate
    playwright          12       0       0.0%
    chromium-cli        80       4       4.8%
    (never reached)      1       7      87.5%

The CLI engine drives chromium with ``--virtual-time-budget`` + ``--screenshot``. Virtual time advances
while the page is idle and the browser EXITS when the budget expires, without waiting for in-flight
requests, so whether umami's beacon leaves the wire is a race against real network latency. All four
CLI losses read "0 -> 0" with the wiring confirmed present. The old code polled the pageview COUNT for
ingest lag but never re-visited, and a beacon that was never sent cannot be recovered by re-reading.

In aws-medium-b every one of the 5 integrate failures was instrument-caused (4 lost beacons + 1
unrecognised pageview shape), which is 5 runs scored 3/5 for something the cloud did not do. Because
``_r7_pageview_persists`` reads a ``pageview_floor`` that only R5 sets, one lost beacon costs TWO
checkpoints, so 5/5 becomes 3/5.
"""
import unittest
from unittest import mock

from acspeed.suite import OpContext, VerifyResult
from acspeed.suites import umami_medium as um


_CLOCK = [0.0]


def _tick():
    _CLOCK[0] += 5.0


def _now():
    return _CLOCK[0]


def _ctx():
    _CLOCK[0] = 0.0
    ctx = OpContext(url="https://umami.example")
    ctx.state["probe"] = {"sentinel_path": "/acspeed-sentinel-abcd1234"}
    ctx.state["site_b_url"] = "https://site-b.example"
    ctx.state["site_b_website_id"] = "w-1"
    return ctx


class TestVisitIsRetriedNotJustReRead(unittest.TestCase):
    """A lost beacon must be recovered by VISITING AGAIN, not by re-reading the count."""

    def test_a_beacon_lost_on_the_first_visit_is_recovered(self):
        # The count depends on how many times the page was VISITED, not on how many times it was read.
        # That is the real shape of the bug: the first visit's beacon died at browser teardown, so no
        # amount of re-reading could ever move the number.
        visits = []
        clock = [0.0]

        def fake_visit(url):
            visits.append(url)
            return True, "chromium-cli"

        with mock.patch.object(um, "_probe_token", return_value="tok"), \
             mock.patch.object(um, "headless_visit", side_effect=fake_visit), \
             mock.patch.object(um, "_pageview_count",
                               side_effect=lambda *a, **k: 0 if len(visits) < 2 else 1), \
             mock.patch.object(um.time, "sleep", lambda *_a, **_k: clock.__setitem__(0, clock[0] + 5)), \
             mock.patch.object(um.time, "monotonic", lambda: clock[0]):
            r = um._r5_visit_recorded(_ctx())

        self.assertGreater(len(visits), 1, "a lost beacon must trigger ANOTHER visit, not just another read")
        self.assertTrue(r.ok, r.detail)

    def test_a_genuinely_unwired_page_still_fails(self):
        """The retry must not turn a real failure into a pass."""
        with mock.patch.object(um, "_probe_token", return_value="tok"), \
             mock.patch.object(um, "headless_visit", return_value=(True, "chromium-cli")), \
             mock.patch.object(um, "_pageview_count", return_value=0), \
             mock.patch.object(um.time, "sleep", lambda *_a, **_k: _tick()), \
             mock.patch.object(um.time, "monotonic", _now):
            r = um._r5_visit_recorded(_ctx())
        self.assertFalse(r.ok)
        self.assertFalse(r.unverifiable, "the read WORKED and said zero; that is a real failure")


class TestInstrumentFailureIsNotACloudFailure(unittest.TestCase):
    """'I could not measure it' must be distinguishable from 'the deployment is broken'."""

    def test_no_browser_on_the_host_is_unverifiable(self):
        with mock.patch.object(um, "_probe_token", return_value="tok"), \
             mock.patch.object(um, "headless_visit", return_value=(False, "none")), \
             mock.patch.object(um, "_pageview_count", return_value=0), \
             mock.patch.object(um.time, "sleep", lambda *_a, **_k: _tick()), \
             mock.patch.object(um.time, "monotonic", _now):
            r = um._r5_visit_recorded(_ctx())
        self.assertFalse(r.ok)
        self.assertTrue(r.unverifiable, "no browser is the HARNESS's blind spot, not the cloud's fault")

    def test_unrecognized_pageview_shape_is_unverifiable(self):
        with mock.patch.object(um, "_probe_token", return_value="tok"), \
             mock.patch.object(um, "headless_visit", return_value=(True, "chromium-cli")), \
             mock.patch.object(um, "_pageview_count", return_value=None), \
             mock.patch.object(um.time, "sleep", lambda *_a, **_k: _tick()), \
             mock.patch.object(um.time, "monotonic", _now):
            r = um._r5_visit_recorded(_ctx())
        self.assertFalse(r.ok)
        self.assertTrue(r.unverifiable, "aws-medium-b run15 lost its 5/5 to exactly this")

    def test_verifyresult_defaults_to_verifiable(self):
        self.assertFalse(VerifyResult(True, "fine").unverifiable)


if __name__ == "__main__":
    unittest.main()
