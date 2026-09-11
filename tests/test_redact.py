"""Credential stripping for session transcripts (acspeed/redact.py, acspeed/sessions.py)."""
import json
import os
import tempfile
import unittest

from acspeed import redact, transcript
from acspeed.sessions import bundle

# Fake substrate ruleset for tests: the public test file must not name real infrastructure.
TEST_RULES = redact.compile_rules(
    ["acmestack", "blobstore", "widgetvm"],
    [r"widget-(?:api|worker)"],
    [r"(?:api|console)\.ctrl\.example", r"node-?0?\d+"],
)


class TestRedactText(unittest.TestCase):
    def test_env_assignment_value_is_stripped_key_kept(self):
        s, c = redact.redact_text("wrote .env: DB_PASSWORD=s3cr3tPazz and MINIO_ROOT_PASSWORD=hunter2xy")
        self.assertNotIn("s3cr3tPazz", s)
        self.assertNotIn("hunter2xy", s)
        self.assertIn("DB_PASSWORD=", s)          # the key stays, so the session is still readable
        self.assertIn("MINIO_ROOT_PASSWORD=", s)
        self.assertEqual(c["value"], 2)

    def test_prefix_and_suffix_keys(self):
        # POSTGRES_ prefix and _BASE / _ID suffix must all still be caught
        s, c = redact.redact_text("SECRET_KEY_BASE=abc123realvalue ACCESS_KEY_ID=AKREALKEY99 X_PASSWORD=p9")
        self.assertNotIn("abc123realvalue", s)
        self.assertNotIn("AKREALKEY99", s)
        self.assertNotIn("=p9", s)
        # a token COUNT name is NOT a secret assignment (the trailing S breaks the match)
        s2, c2 = redact.redact_text("OUTPUT_TOKENS=1234 done")
        self.assertIn("1234", s2)

    def test_shape_based_tokens(self):
        for secret in ("sk-abcdefghij0123456789", "AKIAABCDEFGHIJKLMNOP", "ghp_" + "a" * 21,
                       "eyJhbGciOi.eyJzdWIiOm.sIg0RSflKxw"):
            s, _ = redact.redact_text("token is %s here" % secret)
            self.assertNotIn(secret, s, secret)

    def test_bearer_and_connstring(self):
        s, _ = redact.redact_text("Authorization: Bearer abcDEF123456ghijKL and postgres://u:p4ssw0rd@db:5432/x")
        self.assertNotIn("abcDEF123456ghijKL", s)
        self.assertNotIn("p4ssw0rd", s)
        self.assertIn("postgres://u:", s)          # host/user kept, password gone

    def test_pem_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\nDEF==\n-----END RSA PRIVATE KEY-----"
        s, c = redact.redact_text("key:\n" + pem)
        self.assertNotIn("MIIEabc", s)
        self.assertEqual(c["private-key"], 1)

    def test_non_secret_survives(self):
        # a uuid, an ISO timestamp, and a plain sentence must pass through untouched
        txt = "id 9e196df2-e1a3-470a-81c2-880abb3fbc81 at 2026-08-26T10:00:00.000Z ok"
        s, c = redact.redact_text(txt)
        self.assertEqual(s, txt)
        self.assertEqual(sum(c.values()), 0)

    def test_idempotent(self):
        one, _ = redact.redact_text("API_KEY=supersecretvalue123")
        two, _ = redact.redact_text(one)
        self.assertEqual(one, two)
        self.assertNotIn("]]", two)

    def test_json_keyvalue_secret_in_string(self):
        # a secret that arrives as JSON key:value inside a tool-output STRING (the quote between key and
        # colon defeats KEY=value). Both bare quotes (parsed content) and escaped quotes (serialized).
        bare = '/api/me -> {"token":"BN6a+rif/Nnm5wF4","password":"Acspeed-1669088a-pw"}'
        s, c = redact.redact_text(bare)
        self.assertNotIn("BN6a+rif/Nnm5wF4", s)
        self.assertNotIn("Acspeed-1669088a-pw", s)
        self.assertIn('"token":', s)               # key + structure kept
        self.assertIn('"password":', s)
        self.assertEqual(redact.scan_for_secrets(s), [])   # residue scan agrees
        esc = 'stdout: "{\\"token\\":\\"c6zVCaWAA/LxfAqQ\\"}"'
        s2, _ = redact.redact_text(esc)
        self.assertNotIn("c6zVCaWAA/LxfAqQ", s2)
        self.assertEqual(redact.scan_for_secrets(s2), [])
        # a token COUNT key is not a secret (trailing S breaks the match), so its value survives
        s3, _ = redact.redact_text('{"output_tokens":"1234"}')
        self.assertIn("1234", s3)
        # a TRUNCATED tool output: opening quote, no closing quote (stdout cut off mid-token). The parsed
        # value has no closing quote, but json.dumps adds one on re-serialization -> must still redact.
        trunc = 'authenticated /api/me:\n{"token":"31KtfERbj+heqhdR3E21G/EpmLJnZ1wtFMUxlAfr+iz39'
        s4, _ = redact.redact_text(trunc)
        self.assertNotIn("31KtfERbj", s4)
        self.assertEqual(redact.scan_for_secrets(s4), [])

    def test_prose_password_harvested_and_stripped_everywhere(self):
        # an agent-chosen password echoed in prose/markdown/shell has no key:value shape. It is harvested
        # from one cue-adjacent spot and its literal stripped from EVERY spot (transcript-level pass).
        txt = ("create an admin user with password 'Umami-w7aAAn2u2O8jJj0H'.\n"
               "later: ADMIN PW: Umami-w7aAAn2u2O8jJj0H\n"
               "note: `admin` / `Umami-w7aAAn2u2O8jJj0H`\n"
               "shell: NEWPW='Umami-w7aAAn2u2O8jJj0H'; curl ...\n")
        red, c = redact.redact_transcript(txt)
        self.assertNotIn("Umami-w7aAAn2u2O8jJj0H", red)      # gone from ALL four spots
        self.assertEqual(redact.scan_for_secrets(red), [])   # residue gate agrees
        # a kept app URL / UUID that shares a token substring is NOT stripped (no secret cue precedes it)
        keep = 'deployed at https://umami-acs40e05303-k4ms7bqz.redu.cloud id 76e28485-df97-4cb7-af5e-40f8b98bee81'
        red2, _ = redact.redact_transcript(keep)
        self.assertIn("umami-acs40e05303-k4ms7bqz.redu.cloud", red2)
        self.assertIn("76e28485-df97-4cb7-af5e-40f8b98bee81", red2)

    def test_public_run_token_survives_even_next_to_a_cue(self):
        # the acspeed run token (acs+hex) is PUBLIC (it is in every URL). Even when it appears next to a
        # "token" cue it must NOT be harvested+stripped, or the app URLs get blanked. But an admin
        # password that merely CONTAINS the run token is a different literal and IS still stripped.
        txt = ("source token: acs5fc7ebcf\n"
               "serving at https://umami-acs5fc7ebcf.politepond-847049e8.northeurope.azurecontainerapps.io\n"
               "admin password: Redu-acs5fc7ebcf-Adm1n-2026\n")
        red, _ = redact.redact_transcript(txt)
        self.assertIn("umami-acs5fc7ebcf.politepond-847049e8.northeurope.azurecontainerapps.io", red)
        self.assertNotIn("Redu-acs5fc7ebcf-Adm1n-2026", red)   # the password literal is still gone
        self.assertEqual(redact.scan_for_secrets(red), [])


class TestRedactObj(unittest.TestCase):
    def test_keyed_value_and_token_count_kept(self):
        obj = {"ENCRYPTION_KEY": "abc123def456", "output_tokens": 1234,
               "note": "fine", "nested": {"SECRET": "zzz999"}}
        counts = redact.Counter()
        out = redact.redact_obj(obj, counts)
        self.assertNotIn("abc123def456", json.dumps(out))
        self.assertNotIn("zzz999", json.dumps(out))
        self.assertEqual(out["output_tokens"], 1234)     # count is an int -> never touched
        self.assertEqual(out["note"], "fine")
        self.assertEqual(counts["keyed"], 2)


class TestSubstrate(unittest.TestCase):
    def test_infra_names_hosts_and_ips_removed(self):
        s, c = redact.scrub_substrate(
            "booting AcmeStack WidgetVM on Blobstore, widget-api, node01; "
            "api.ctrl.example and console.ctrl.example; VM at 10.1.0.145 and 10.50.0.40", rules=TEST_RULES)
        for infra in ("AcmeStack", "WidgetVM", "Blobstore", "widget-api", "node01",
                      "api.ctrl.example", "console.ctrl.example", "10.1.0.145", "10.50.0.40"):
            self.assertNotIn(infra, s, infra)
        self.assertIn("[infra]", s)
        self.assertIn("[infra-host]", s)
        self.assertIn("[ip]", s)

    def test_keeps_app_url_and_localhost_and_brand(self):
        s, _ = redact.scrub_substrate("deployed https://myapp-ab12cd.redu.cloud, bound 127.0.0.1, redu ok",
                                      rules=TEST_RULES)
        self.assertIn("https://myapp-ab12cd.redu.cloud", s)   # the app the agent deployed stays
        self.assertIn("127.0.0.1", s)                          # localhost stays (meaningful, not infra)
        self.assertIn("redu", s)                               # platform brand stays

    def test_scan_for_substrate(self):
        self.assertTrue(redact.scan_for_substrate("ran on AcmeStack at 10.0.0.5", rules=TEST_RULES))
        clean, _ = redact.scrub_substrate("ran on AcmeStack at 10.0.0.5", rules=TEST_RULES)
        self.assertEqual(redact.scan_for_substrate(clean, rules=TEST_RULES), [])

    def test_no_rules_means_generic_ip_only(self):
        # a public checkout has no private denylist: names are kept, but IPs are always scrubbed
        empty = redact.compile_rules([], [], [])
        s, _ = redact.scrub_substrate("AcmeStack at 10.0.0.5", rules=empty)
        self.assertIn("AcmeStack", s)
        self.assertNotIn("10.0.0.5", s)


class TestPreservesTimingAndTokens(unittest.TestCase):
    """The load-bearing invariant: a redacted transcript reconstructs the SAME trace + token totals."""

    def _write_transcript(self, path):
        rows = [
            {"type": "assistant", "timestamp": "2026-08-26T10:00:00.000Z", "requestId": "r1", "uuid": "u1",
             "message": {"id": "m1", "usage": {"input_tokens": 100, "output_tokens": 20,
                                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                         "content": [{"type": "text", "text": "deploying, will set DB_PASSWORD=s3cr3tPazz"}]}},
            {"type": "user", "timestamp": "2026-08-26T10:00:05.000Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "wrote .env API_KEY=sk-abcdefghij0123456789 SECRET=zzztopsecret"
                                                 " on AcmeStack WidgetVM, VM 10.1.0.145"}]}},
            {"type": "assistant", "timestamp": "2026-08-26T10:00:07.000Z", "requestId": "r2", "uuid": "u2",
             "message": {"id": "m2", "usage": {"input_tokens": 200, "output_tokens": 40,
                                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                         "content": [{"type": "text", "text": "served"}]}},
            {"type": "user", "timestamp": "2026-08-26T10:00:12.000Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}},
        ]
        with open(path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_trace_and_tokens_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            orig = os.path.join(d, "orig.jsonl")
            self._write_transcript(orig)
            red_text, counts = redact.redact_transcript(open(orig).read(), substrate=True, rules=TEST_RULES)
            redp = os.path.join(d, "red.jsonl")
            with open(redp, "w") as fh:
                fh.write(red_text)
            # secrets AND substrate gone, structure/time intact
            self.assertNotIn("s3cr3tPazz", red_text)
            self.assertNotIn("sk-abcdefghij0123456789", red_text)
            self.assertNotIn("zzztopsecret", red_text)
            self.assertNotIn("AcmeStack", red_text)
            self.assertNotIn("10.1.0.145", red_text)
            self.assertIn("2026-08-26T10:00:05.000Z", red_text)   # timestamps preserved
            for line in red_text.splitlines():                    # still valid JSON per line
                json.loads(line)
            # the reference core computes the SAME numbers on the redacted session (ignore the filename)
            def _timing(p):
                return {k: v for k, v in transcript.lane_summary(p).items() if k != "path"}
            self.assertEqual(_timing(orig), _timing(redp))
            self.assertEqual(transcript.dedupe_token_usage(orig), transcript.dedupe_token_usage(redp))
            self.assertGreater(sum(counts.values()), 0)

    def test_json_keys_are_never_rewritten(self):
        """Regression: the harvest cue matched INSIDE a compound identifier.

        ``bearerTokenAuthenticationEnabled`` gave cue ``token`` + value ``AuthenticationEnabled``, and the
        global literal strip then blanked that substring inside the JSON KEY, changing the message shape
        this module promises to preserve. Real AWS CloudWatch output carries this key.
        """
        row = {"type": "user", "timestamp": "2026-08-26T10:00:05.000Z",
               "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                        "content": "listed log groups"}]},
               "mcpMeta": {"logGroups": [{"logGroupName": "/aws/lambda/x",
                                          "bearerTokenAuthenticationEnabled": False}]}}
        text = json.dumps(row) + "\n"
        red, _ = redact.redact_transcript(text, substrate=False)
        self.assertIn("bearerTokenAuthenticationEnabled", red)
        self.assertEqual(sorted(json.loads(red)["mcpMeta"]["logGroups"][0]),
                         sorted(row["mcpMeta"]["logGroups"][0]))


class TestBundle(unittest.TestCase):
    def test_bundle_writes_manifest_and_verifies_clean(self):
        with tempfile.TemporaryDirectory() as d:
            ind, outd = os.path.join(d, "in"), os.path.join(d, "out")
            os.makedirs(ind)
            with open(os.path.join(ind, "a.jsonl"), "w") as fh:
                fh.write(json.dumps({"type": "assistant", "timestamp": "2026-08-26T10:00:00Z",
                                     "message": {"content": [{"type": "text",
                                                              "text": "PASSWORD=leakme123 on AcmeStack 10.0.0.9"}]}}) + "\n")
                fh.write(json.dumps({"type": "user", "timestamp": "2026-08-26T10:00:03Z",
                                     "message": {"content": [{"type": "tool_result", "tool_use_id": "t",
                                                              "content": "ok"}]}}) + "\n")
            report = bundle(ind, outd, rules=TEST_RULES)
            self.assertEqual(report["inputs"], 1)
            self.assertEqual(report["written"], 1)
            self.assertEqual(len(report["files_with_residue"]), 0)
            self.assertTrue(os.path.exists(os.path.join(outd, "REDACTION-MANIFEST.json")))
            self.assertTrue(os.path.exists(os.path.join(outd, "README.md")))
            body = open(os.path.join(outd, "a.jsonl")).read()
            self.assertNotIn("leakme123", body)
            self.assertNotIn("AcmeStack", body)
            self.assertNotIn("10.0.0.9", body)
            self.assertEqual(redact.scan_for_secrets(body), [])
            self.assertEqual(redact.scan_for_substrate(body, rules=TEST_RULES), [])

    def test_bundle_raises_on_no_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                bundle(os.path.join(d, "empty"), os.path.join(d, "out"))


if __name__ == "__main__":
    unittest.main()
