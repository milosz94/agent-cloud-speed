"""A benchmark defined in JSON must behave exactly like one written in Python.

--custom exists so adding operations is not a code change. These tests run the generated verifies
against a real local HTTP server, because a declarative verify that compiles but reads nothing is the
failure mode that costs a paid run to discover.
"""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from acspeed.suite import OpContext
from acspeed.suites import custom

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _App(BaseHTTPRequestHandler):
    widgets = []

    def do_GET(self):
        if self.path == "/":
            body = b"<html><title>my-app</title></html>"
            self.send_response(200)
        elif self.path == "/api/widgets":
            body = json.dumps(self.widgets).encode()
            self.send_response(200)
        elif self.path == "/boom":
            body = b"server error"
            self.send_response(503)
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class CustomSuiteFromJson(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _App)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _inst(self, doc):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(doc, fh); path = fh.name
        try:
            return custom.load(path)
        finally:
            os.unlink(path)

    # ---- the shipped example -------------------------------------------------

    def test_shipped_example_loads(self):
        i = custom.load(os.path.join(ROOT, "examples", "custom-benchmark.json"))
        self.assertEqual([o.op_id for o in i.operations], ["deploy-serve", "create-widget"])
        self.assertEqual(i.operations[1].max_attempts, 2)
        self.assertIsNotNone(i.durability)
        self.assertIsNotNone(i.durability.liveness)

    # ---- the verifies actually read a live server ----------------------------

    def test_body_contains_passes_against_the_real_app(self):
        i = self._inst({"name": "t", "operations": [
            {"op_id": "d", "op_type": "provision", "task": "deploy",
             "verify": {"path": "/", "body_contains": "my-app"}}]})
        r = i.operations[0].verify(OpContext(url=self.url))
        self.assertTrue(r.ok, r.detail)

    def test_body_contains_fails_when_the_string_is_absent(self):
        i = self._inst({"name": "t", "operations": [
            {"op_id": "d", "op_type": "provision", "task": "deploy",
             "verify": {"path": "/", "body_contains": "someone-elses-app"}}]})
        self.assertFalse(i.operations[0].verify(OpContext(url=self.url)).ok)

    def test_json_contains_matches_an_element_of_a_list(self):
        _App.widgets = [{"name": "other"}, {"name": "acs-widget-01"}]
        i = self._inst({"name": "t", "operations": [
            {"op_id": "w", "op_type": "operate_mutate", "task": "make it",
             "verify": {"path": "/api/widgets", "status": 200,
                        "json_contains": {"name": "acs-widget-01"}}}]})
        self.assertTrue(i.operations[0].verify(OpContext(url=self.url)).ok)

    def test_json_contains_fails_when_absent(self):
        _App.widgets = [{"name": "other"}]
        i = self._inst({"name": "t", "operations": [
            {"op_id": "w", "op_type": "operate_mutate", "task": "make it",
             "verify": {"path": "/api/widgets", "json_contains": {"name": "acs-widget-01"}}}]})
        self.assertFalse(i.operations[0].verify(OpContext(url=self.url)).ok)

    def test_status_below_catches_a_5xx(self):
        i = self._inst({"name": "t", "operations": [
            {"op_id": "d", "op_type": "provision", "task": "deploy",
             "verify": {"path": "/boom", "status_below": 500}}]})
        self.assertFalse(i.operations[0].verify(OpContext(url=self.url)).ok)

    def test_body_matches_regex(self):
        i = self._inst({"name": "t", "operations": [
            {"op_id": "d", "op_type": "provision", "task": "deploy",
             "verify": {"path": "/", "body_matches": "<title>my-[a-z]+</title>"}}]})
        self.assertTrue(i.operations[0].verify(OpContext(url=self.url)).ok)

    # ---- the status-0 trap ---------------------------------------------------

    def test_a_dead_url_never_verifies(self):
        """curl reports a refused connection as 0, and 0 < 500. A generated verify must not pass."""
        i = self._inst({"name": "t", "operations": [
            {"op_id": "d", "op_type": "provision", "task": "deploy",
             "verify": {"path": "/", "status_below": 500}}]})
        r = i.operations[0].verify(OpContext(url="http://127.0.0.1:9"))
        self.assertFalse(r.ok)
        self.assertIn("nothing answered", r.detail)

    def test_no_serving_url_is_unverifiable(self):
        i = self._inst({"name": "t", "operations": [
            {"op_id": "d", "op_type": "provision", "task": "deploy",
             "verify": {"path": "/"}}]})
        r = i.operations[0].verify(OpContext(url=None))
        self.assertFalse(r.ok)
        self.assertTrue(r.unverifiable)

    # ---- errors are caught before a run, with a usable message ---------------

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(custom.CustomSuiteError) as e:
            self._inst({"name": "t", "opperations": []})
        self.assertIn("unknown key", str(e.exception))

    def test_bad_op_type_names_the_allowed_values(self):
        with self.assertRaises(custom.CustomSuiteError) as e:
            self._inst({"name": "t", "operations": [
                {"op_id": "d", "op_type": "deploy", "task": "x", "verify": {"path": "/"}}]})
        self.assertIn("provision", str(e.exception))

    def test_dangling_depends_on_is_rejected(self):
        with self.assertRaises(custom.CustomSuiteError) as e:
            self._inst({"name": "t", "operations": [
                {"op_id": "b", "op_type": "operate_mutate", "task": "x",
                 "verify": {"path": "/"}, "depends_on": ["nope"]}]})
        self.assertIn("nope", str(e.exception))

    def test_duplicate_op_id_is_rejected(self):
        with self.assertRaises(custom.CustomSuiteError) as e:
            self._inst({"name": "t", "operations": [
                {"op_id": "d", "op_type": "provision", "task": "x", "verify": {"path": "/"}},
                {"op_id": "d", "op_type": "operate_mutate", "task": "y", "verify": {"path": "/"}}]})
        self.assertIn("duplicate", str(e.exception))

    def test_verify_without_path_is_rejected(self):
        with self.assertRaises(custom.CustomSuiteError) as e:
            self._inst({"name": "t", "operations": [
                {"op_id": "d", "op_type": "provision", "task": "x", "verify": {"status": 200}}]})
        self.assertIn("path", str(e.exception))

    def test_empty_task_is_rejected(self):
        with self.assertRaises(custom.CustomSuiteError) as e:
            self._inst({"name": "t", "operations": [
                {"op_id": "d", "op_type": "provision", "task": "  ", "verify": {"path": "/"}}]})
        self.assertIn("task", str(e.exception))

    def test_missing_file_says_so(self):
        with self.assertRaises(custom.CustomSuiteError) as e:
            custom.load("/nonexistent/benchmark.json")
        self.assertIn("no such custom benchmark file", str(e.exception))

    def test_invalid_json_says_so(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json"); path = fh.name
        try:
            with self.assertRaises(custom.CustomSuiteError) as e:
                custom.load(path)
            self.assertIn("not valid JSON", str(e.exception))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
