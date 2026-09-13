"""A connection string without its host must still lose its password.

This is a positive control for a hole that reached the published bundle. _CONN required a trailing
'@' as its terminator, so a connection string truncated before the host (which cloud logs print
routinely) never matched. Nine real database passwords shipped in published transcripts, and because
the residue re-scan used the same rule, all twelve manifests reported residue 0 while the secrets sat
there. A check that cannot fail on the case that got through is not a check.
"""
import unittest

from acspeed.redact import redact_text, scan_for_secrets

SECRET = "Dlvk9dscABHXaJmQMN3unaOZOvgd"


class ConnectionStringsWithoutAHost(unittest.TestCase):

    def test_password_is_removed_without_a_trailing_at(self):
        out, _ = redact_text(f"postgresql://postgres:{SECRET}")
        self.assertNotIn(SECRET, out)
        self.assertIn("REDACTED", out)

    def test_password_is_still_removed_with_a_host(self):
        out, _ = redact_text(f"postgresql://postgres:{SECRET}@db.example.com:5432/umami")
        self.assertNotIn(SECRET, out)
        self.assertIn("db.example.com", out, "only the password should go")

    def test_the_residue_scan_sees_it_too(self):
        """The scan is what attests the bundle; it must fail on the same input the redactor fixes."""
        self.assertTrue(scan_for_secrets(f"postgresql://postgres:{SECRET}"),
                        "an unredacted password must be reported as residue")
        self.assertFalse(scan_for_secrets(redact_text(f"postgresql://postgres:{SECRET}")[0]))

    def test_it_survives_json_escaping(self):
        out, _ = redact_text(f'"input": "postgresql://umami:{SECRET}"')
        self.assertNotIn(SECRET, out)

    def test_other_database_schemes(self):
        for scheme in ("mysql", "mongodb", "mongodb+srv", "redis", "rediss", "amqp", "clickhouse"):
            out, _ = redact_text(f"{scheme}://user:{SECRET}")
            self.assertNotIn(SECRET, out, scheme)

    def test_a_port_is_not_mistaken_for_a_password(self):
        """Without the '@' anchor a wildcard scheme would redact every port in the corpus."""
        for url in ("http://example.com:8080/health", "https://api.example.com:443/v1",
                    "http://10.0.2.2:3000"):
            out, _ = redact_text(url)
            self.assertEqual(out, url, url)


if __name__ == "__main__":
    unittest.main()
