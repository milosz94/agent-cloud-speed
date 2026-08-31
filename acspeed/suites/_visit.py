"""A headless-browser visit for the Medium integration read.

The integration read must prove, end to end, that a real visit to the second site is recorded in the
first app (umami). The only way that is genuine is if the RUNNER, not the agent, produces the visit:
the pageview must exist because the harness's own browser rendered the instrumented page and its
client-side umami snippet fired its beacon, never because the agent self-reported it or curled umami's
collect API. So this loads the page in a real headless browser and lets its JavaScript run.

Playwright is used if installed (it waits for the network to settle, so the beacon has been sent),
otherwise a headless chromium/chrome CLI (a throwaway screenshot forces a full render + JS execution).
If NEITHER is present the visit cannot be performed; the caller must treat that as "unverified", never
a pass. Nothing here raises: a verify predicate is the observe primitive and must not throw into a
timed operation.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile

_CHROME_BINS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome")


def _ms_playwright_chromes() -> list:
    """The chromium binaries Playwright downloaded (~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome).
    Preferred CLI browsers because they are UNCONFINED (unlike a snap chromium, which cannot write its
    screenshot outside its sandbox and so silently fails the render) and are present whenever
    ``playwright install chromium`` has run - so a run works even when the Playwright PYTHON package is not
    importable in the invocation's interpreter (the exact failure that made every Medium run report 'no
    headless browser' on 2026-08-31: a pyenv python without playwright + only a confined snap chromium)."""
    base = os.path.expanduser("~/.cache/ms-playwright")
    bins = glob.glob(os.path.join(base, "chromium-*", "chrome-linux*", "chrome"))
    return sorted(bins, reverse=True)  # newest build first

# A REAL browser User-Agent. Analytics apps (umami, plausible, ...) run isbot() on the request UA and
# SILENTLY DROP the event for a bot-looking one; Playwright/headless-chromium default to a UA containing
# "HeadlessChrome", which isbot flags -> the pageview never records (umami answers {"beep":"boop"}).
# Sending a normal Chrome UA makes the tracked visit count, which is the whole point of the read.
_REAL_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0.0.0 Safari/537.36")


def _visit_playwright(url: str, settle_ms: int):
    """True/False if Playwright ran/failed; None if Playwright is not installed."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415 (optional dep)
    except Exception:  # noqa: BLE001
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_context(ignore_https_errors=True, user_agent=_REAL_UA).new_page()
                page.goto(url, wait_until="load", timeout=settle_ms + 20000)
                try:
                    page.wait_for_load_state("networkidle", timeout=settle_ms)
                except Exception:  # noqa: BLE001 - networkidle is a nicety; the beacon usually beats it
                    pass
            finally:
                browser.close()
        return True
    except Exception:  # noqa: BLE001 - a browser crash is not a false pass
        return False


def _visit_chrome_cli(url: str, settle_ms: int) -> bool:
    # chromium needs an action to force a full render + JS run; a throwaway screenshot does it, and
    # --virtual-time-budget keeps the page alive long enough for the umami beacon to be sent. Prefer the
    # unconfined ms-playwright binaries; fall back to whatever chromium is on PATH (a snap build is confined
    # and usually cannot write the screenshot, so it degrades to the next candidate).
    candidates = _ms_playwright_chromes() + [shutil.which(b) for b in _CHROME_BINS]
    for binp in candidates:
        if not binp or not os.path.exists(binp):
            continue
        with tempfile.TemporaryDirectory() as td:
            shot = os.path.join(td, "visit.png")
            cmd = [binp, "--headless=new", "--disable-gpu", "--no-sandbox",
                   f"--user-agent={_REAL_UA}",
                   f"--virtual-time-budget={settle_ms}", f"--screenshot={shot}", url]
            try:
                subprocess.run(cmd, capture_output=True, timeout=settle_ms / 1000 + 30)
            except Exception:  # noqa: BLE001
                continue
            if os.path.exists(shot) and os.path.getsize(shot) > 0:
                return True
    return False


def headless_visit(url: str, settle_s: float = 8.0):
    """Load ``url`` in a real headless browser so its JS runs (the umami beacon fires).

    Returns ``(ok, engine)``: ``ok`` True iff a browser actually rendered the page; ``engine`` is
    ``"playwright"`` / ``"chromium-cli"`` / ``"none"`` (no browser available -> caller degrades to
    unverified, never a pass)."""
    ms = int(settle_s * 1000)
    r = _visit_playwright(url, ms)
    if r is True:
        return True, "playwright"
    if r is False:
        return False, "playwright-error"
    # r is None: Playwright not installed -> try a headless chrome CLI.
    if _visit_chrome_cli(url, ms):
        return True, "chromium-cli"
    return False, "none"
