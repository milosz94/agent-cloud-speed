"""Adapter config is materialized, never demanded.

The user should never hand-edit a config file for the common case. Every setup step a machine can
do, acspeed does; it refuses only when a value genuinely cannot be discovered, and then it prints
the single command that supplies it. These tests pin that contract, because the failure mode it
replaces (a refused run after the agent was launched) costs real money to discover.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(code, data_dir, env=None, path_prefix=None):
    e = dict(os.environ)
    for v in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "GCP_PROJECT", "CLOUDSDK_CORE_PROJECT"):
        e.pop(v, None)
    e["ACSPEED_DATA"] = data_dir
    if path_prefix:
        e["PATH"] = path_prefix + os.pathsep + e.get("PATH", "")
    if env:
        e.update(env)
    return subprocess.run([sys.executable, "-c", code], cwd=HERE, env=e,
                          capture_output=True, text=True, timeout=120)


class ConfigIsAutomatic(unittest.TestCase):

    def test_created_on_first_use_with_no_user_action(self):
        with tempfile.TemporaryDirectory() as d:
            r = _run("import autorun; autorun.ensure_adapter_config('aws')", d)
            self.assertEqual(r.returncode, 0, r.stderr)
            cfg = os.path.join(d, "_config", "aws.mcp.json")
            self.assertTrue(os.path.exists(cfg), "config was not created")
            json.load(open(cfg))  # must be valid JSON, not a half-written file

    def test_gcp_project_is_filled_from_the_environment(self):
        with tempfile.TemporaryDirectory() as d:
            r = _run("import autorun; autorun.ensure_adapter_config('gcp')", d,
                     env={"GOOGLE_CLOUD_PROJECT": "proj-from-env"})
            self.assertEqual(r.returncode, 0, r.stderr)
            cfg = json.load(open(os.path.join(d, "_config", "gcp.mcp.json")))
            env = cfg["mcpServers"]["cloud-run"]["env"]
            self.assertEqual(env["GOOGLE_CLOUD_PROJECT"], "proj-from-env")

    def test_gcp_project_is_filled_from_gcloud(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as b:
            stub = os.path.join(b, "gcloud")
            with open(stub, "w") as fh:
                fh.write("#!/bin/sh\necho proj-from-gcloud\n")
            os.chmod(stub, 0o755)
            r = _run("import autorun; autorun.ensure_adapter_config('gcp')", d, path_prefix=b)
            self.assertEqual(r.returncode, 0, r.stderr)
            cfg = json.load(open(os.path.join(d, "_config", "gcp.mcp.json")))
            self.assertEqual(cfg["mcpServers"]["cloud-run"]["env"]["GOOGLE_CLOUD_PROJECT"],
                             "proj-from-gcloud")

    def test_no_placeholder_ever_survives_into_a_written_config(self):
        """The old failure: a template was copied unedited, so the run reached the cloud and died
        there, after the clock and the money started."""
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as b:
            stub = os.path.join(b, "gcloud")
            with open(stub, "w") as fh:
                fh.write("#!/bin/sh\necho '(unset)'\n")   # gcloud's own "no project selected"
            os.chmod(stub, 0o755)
            r = _run("import autorun; autorun.ensure_adapter_config('gcp')", d, path_prefix=b)
            self.assertNotEqual(r.returncode, 0, "should refuse when the project cannot be found")
            self.assertIn("gcloud config set project", r.stdout + r.stderr,
                          "the refusal must name the command that fixes it")
            self.assertFalse(os.path.exists(os.path.join(d, "_config", "gcp.mcp.json")),
                             "refused, so nothing may be written")

    def test_a_stale_placeholder_is_repaired_not_rejected(self):
        """A user who set their gcloud project AFTER the first run must not stay stuck."""
        with tempfile.TemporaryDirectory() as d:
            cfgdir = os.path.join(d, "_config")
            os.makedirs(cfgdir)
            cfg = os.path.join(cfgdir, "gcp.mcp.json")
            with open(cfg, "w") as fh:
                fh.write(open(os.path.join(HERE, "config", "gcp.mcp.json")).read())
            self.assertIn("REPLACE_WITH", open(cfg).read())
            r = _run("import autorun; autorun.ensure_adapter_config('gcp')", d,
                     env={"GOOGLE_CLOUD_PROJECT": "proj-later"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("REPLACE_WITH", open(cfg).read())
            self.assertEqual(json.load(open(cfg))["mcpServers"]["cloud-run"]["env"]
                             ["GOOGLE_CLOUD_PROJECT"], "proj-later")

    def test_no_temp_file_is_left_behind(self):
        with tempfile.TemporaryDirectory() as d:
            _run("import autorun; autorun.ensure_adapter_config('azure')", d)
            leftovers = [f for f in os.listdir(os.path.join(d, "_config")) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [], f"atomic write leaked {leftovers}")


if __name__ == "__main__":
    unittest.main()
