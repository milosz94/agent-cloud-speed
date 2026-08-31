"""The headless-visit browser selection (acspeed/suites/_visit.py).

The Medium integration read drives a REAL headless visit and must never fake it. On 2026-08-31 every
Medium run reported 'no headless browser' because the invocation's python could not import Playwright
and the only chromium on PATH was a confined snap build (which cannot write its screenshot). The fix:
prefer the unconfined ms-playwright chromium binaries in the CLI fallback, so a run works even without
the Playwright python package. These tests pin that selection without launching a real browser.
"""
import os
import tempfile
import unittest
from unittest import mock

from acspeed.suites import _visit


def _screenshot_arg(cmd):
    return next((a.split("=", 1)[1] for a in cmd if a.startswith("--screenshot=")), None)


class TestBrowserFallback(unittest.TestCase):
    def test_cli_uses_ms_playwright_binary(self):
        # the ms-playwright chromium renders (writes a screenshot) -> the CLI fallback succeeds even though
        # the Playwright python package is never touched.
        with tempfile.TemporaryDirectory() as home:
            msbin = os.path.join(home, "ms-chrome")
            open(msbin, "w").close()

            def fake_run(cmd, **k):
                shot = _screenshot_arg(cmd)
                if cmd[0] == msbin and shot:
                    with open(shot, "wb") as f:
                        f.write(b"PNGDATA")            # non-empty => rendered
                return mock.Mock()

            with mock.patch.object(_visit, "_ms_playwright_chromes", return_value=[msbin]), \
                 mock.patch.object(_visit.shutil, "which", return_value=None), \
                 mock.patch.object(_visit.subprocess, "run", side_effect=fake_run):
                self.assertTrue(_visit._visit_chrome_cli("https://x", 3000))

    def test_confined_snap_only_fails(self):
        # only a confined snap chromium is available and it writes NO screenshot -> the CLI fallback fails
        # (which is the exact 'none' outcome that must fail the read, never a false pass).
        with tempfile.TemporaryDirectory() as home:
            snap = os.path.join(home, "snap-chromium")
            open(snap, "w").close()

            def fake_run(cmd, **k):
                return mock.Mock()                    # writes nothing (confined)

            with mock.patch.object(_visit, "_ms_playwright_chromes", return_value=[]), \
                 mock.patch.object(_visit.shutil, "which",
                                   side_effect=lambda b: snap if b == "chromium" else None), \
                 mock.patch.object(_visit.subprocess, "run", side_effect=fake_run):
                self.assertFalse(_visit._visit_chrome_cli("https://x", 3000))

    def test_headless_visit_uses_cli_when_playwright_package_missing(self):
        with mock.patch.object(_visit, "_visit_playwright", return_value=None), \
             mock.patch.object(_visit, "_visit_chrome_cli", return_value=True):
            self.assertEqual(_visit.headless_visit("https://x"), (True, "chromium-cli"))

    def test_headless_visit_none_when_nothing_works(self):
        with mock.patch.object(_visit, "_visit_playwright", return_value=None), \
             mock.patch.object(_visit, "_visit_chrome_cli", return_value=False):
            self.assertEqual(_visit.headless_visit("https://x"), (False, "none"))

    def test_playwright_launch_error_is_not_none(self):
        # a playwright that IMPORTS but crashes on launch is distinct from 'none' (a real error, surfaced).
        with mock.patch.object(_visit, "_visit_playwright", return_value=False):
            self.assertEqual(_visit.headless_visit("https://x"), (False, "playwright-error"))


if __name__ == "__main__":
    unittest.main()
