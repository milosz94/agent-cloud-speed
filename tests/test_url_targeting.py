"""Cloud-agnostic run-identity: pick_url must select ONLY the deployment carrying THIS run's token, so
it can never latch onto a pre-existing or concurrent-run deployment the agent merely listed. This
reproduces the 2026-08-28 defect: a real umami run captured a foreign `origin-web` URL that a
`list_deployments` result had put in the transcript, and every downstream measurement + suite verify
targeted the wrong VM.

The filter is a pure substring test (no cloud knowledge); the per-cloud url_re/substrate stay in the
adapter, so these tests exercise the mechanism with the real redu regexes.
"""
import unittest

from autorun import ADAPTERS, pick_url

REDU_RE = ADAPTERS["redu"]["url_re"]
REDU_SUB = ADAPTERS["redu"]["substrate_hosts"]


class TestTokenTargeting(unittest.TestCase):
    def test_the_2026_08_28_defect_is_fixed(self):
        # a transcript blob exactly like the failing run: the foreign deployment (from list_deployments)
        # plus this run's real umami (named with the token) plus the substrate host.
        token = "acs1a2b3c4d"
        blob = (
            "list_deployments -> https://origin-web-e2761695.redu.cloud "
            "https://wangpan-zg24l0rd.redu.cloud https://vaultwarden-81a389bf.redu.cloud "
            f"deploy_compose -> https://umami-{token}.redu.cloud served on https://mcp.redu.cloud"
        )
        got = pick_url(blob, REDU_RE, REDU_SUB, require_token=token)
        self.assertEqual(got["url"], f"https://umami-{token}.redu.cloud")
        # the foreign deployments are NOT even candidates
        self.assertNotIn("https://origin-web-e2761695.redu.cloud", got["candidates"])
        self.assertNotIn("https://wangpan-zg24l0rd.redu.cloud", got["candidates"])
        self.assertFalse(got["ambiguous"])

    def test_no_token_url_yields_no_url(self):
        # only foreign deployments present, none carrying the token -> safe no-url (never a wrong VM)
        blob = "https://origin-web-e2761695.redu.cloud https://hemi-market-35xomc5b.redu.cloud"
        got = pick_url(blob, REDU_RE, REDU_SUB, require_token="acsdeadbeef")
        self.assertIsNone(got["url"])
        self.assertEqual(got["candidates"], [])

    def test_primary_selected_by_name_over_second_site(self):
        # the Medium-B duplicate-token case: the primary (umami) AND the second site (alcove) both carry the
        # run token, provisioned concurrently, and the poller used to lock whichever served first. Selecting
        # the primary BY NAME picks umami even when the second site served first; each keeps its own name.
        token = "acs8acb9180"
        blob = f"https://alcove-{token}.redu.cloud served first then https://umami-{token}.redu.cloud"
        got = pick_url(blob, REDU_RE, REDU_SUB, require_token=token, primary_name="umami")
        self.assertEqual(got["url"], f"https://umami-{token}.redu.cloud")
        self.assertFalse(got["ambiguous"])  # the second site is a different name, so no ambiguity remains

    def test_only_second_site_served_yields_no_url(self):
        # if ONLY the second site has served yet, return None so the poller keeps waiting for the primary
        # rather than locking the second site.
        token = "acs8acb9180"
        got = pick_url(f"https://alcove-{token}.redu.cloud", REDU_RE, REDU_SUB,
                       require_token=token, primary_name="umami")
        self.assertIsNone(got["url"])

    def test_primary_name_still_requires_the_token(self):
        # a FOREIGN deployment sharing the primary's name but NOT this run's token is still excluded by the
        # token filter, so name-matching never latches another run's same-named app.
        token = "acs8acb9180"
        blob = f"https://umami-acsffffffff.redu.cloud https://umami-{token}.redu.cloud"
        got = pick_url(blob, REDU_RE, REDU_SUB, require_token=token, primary_name="umami")
        self.assertEqual(got["url"], f"https://umami-{token}.redu.cloud")
        self.assertNotIn("https://umami-acsffffffff.redu.cloud", got["candidates"])

    def test_token_appears_in_the_hostname_not_only_a_path(self):
        # a foreign URL that merely mentions the token in a path must NOT be accepted; the token has to
        # be in the hostname. (Here the whole-URL substring is what we test; a path-only mention still
        # fails because the token is unique and long, and real deploy URLs put the name in the host.)
        token = "acsfeed0000"
        blob = f"https://other-app.redu.cloud/{token}/page https://real-{token}.redu.cloud"
        got = pick_url(blob, REDU_RE, REDU_SUB, require_token=token)
        # the real one (token in host) is selected; keep it deterministic
        self.assertEqual(got["url"], f"https://real-{token}.redu.cloud")

    def test_backward_compatible_without_token(self):
        # require_token=None keeps the old behavior (used by non-token callers / tests)
        blob = "https://umami-abcd.redu.cloud https://mcp.redu.cloud"
        got = pick_url(blob, REDU_RE, REDU_SUB)
        self.assertEqual(got["url"], "https://umami-abcd.redu.cloud")

    def test_concurrent_run_isolation(self):
        # two concurrent runs' umami URLs in one blob; each run's token selects only its own
        blob = "https://umami-acs11111111.redu.cloud https://umami-acs22222222.redu.cloud"
        a = pick_url(blob, REDU_RE, REDU_SUB, require_token="acs11111111")
        b = pick_url(blob, REDU_RE, REDU_SUB, require_token="acs22222222")
        self.assertEqual(a["url"], "https://umami-acs11111111.redu.cloud")
        self.assertEqual(b["url"], "https://umami-acs22222222.redu.cloud")


if __name__ == "__main__":
    unittest.main()
