"""The built-vs-priced AUDIT (acspeed/aws_audit.py): the independent cross-check that the run-rate priced
every billable resource the agent actually provisioned. Reads what was BUILT from the transcript's real
tool-call create operations (never a text grep over stale workdir content) and compares to what was PRICED.
"""
import json
import os
import tempfile
import unittest

from acspeed.aws_audit import (billable_creates_from_transcript, priced_kinds_from_components, audit_run)


def _transcript(*tool_calls):
    """Write a minimal Claude-session .jsonl: each (tool_name, field, text) becomes one tool_use turn."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        for name, field, text in tool_calls:
            row = {"message": {"content": [{"type": "tool_use", "name": name,
                                            "id": "t", "input": {field: text}}]}}
            fh.write(json.dumps(row) + "\n")
    return path


class TestBuiltFromTranscript(unittest.TestCase):
    def test_counts_boto3_and_cli_creates_from_tool_calls(self):
        t = _transcript(
            ("mcp__aws-mcp__aws___run_script", "code",
             "await call_boto3(service_name='elbv2', operation_name='CreateLoadBalancer', params={})"),
            ("mcp__aws-mcp__aws___run_script", "code",
             "await call_boto3(operation_name='CreateLoadBalancer')"),           # a second ALB
            ("Bash", "command", "aws rds create-db-instance --db-instance-identifier umami-db-acs1 ..."),
            ("Bash", "command", "aws ec2 run-instances --image-id ami-1 --count 1"),
            ("Bash", "command", "aws ec2 create-security-group --group-name x"),  # NON-billable: ignored
        )
        try:
            built = billable_creates_from_transcript(t)
        finally:
            os.unlink(t)
        self.assertEqual(built["load_balancer"], 2)
        self.assertEqual(built["rds"], 1)
        self.assertEqual(built["ec2"], 1)
        self.assertNotIn("security_group", built)                # non-billable create is not counted

    def test_a_read_of_a_file_containing_the_word_is_not_a_build(self):
        # only the EXECUTED command/code is parsed; a Read tool's content is never inspected
        t = _transcript(("Read", "file_path", "/app/deploy.md that mentions create-load-balancer everywhere"))
        try:
            built = billable_creates_from_transcript(t)
        finally:
            os.unlink(t)
        self.assertEqual(dict(built), {})                        # nothing built


class TestPricedKinds(unittest.TestCase):
    def test_component_names_map_to_kinds(self):
        priced = priced_kinds_from_components(
            [{"name": "load_balancer"}, {"name": "compute"}, {"name": "compute:rds"},
             {"name": "storage:rds"}, {"name": "compute:lightsail-container"}])
        self.assertEqual(priced["load_balancer"], 1)
        self.assertEqual(priced["ec2"], 1)
        self.assertEqual(priced["rds"], 2)                       # compute:rds + storage:rds both -> rds
        self.assertEqual(priced["lightsail"], 1)


class TestAuditRun(unittest.TestCase):
    def _run_json(self, components, transcript):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump({"run": 11, "run_token": "acs1", "url": "http://x",
                       "cost_run_rate": {"components": components},
                       "deploy": {"transcript": transcript}}, fh)
        return path

    def test_flags_underpriced_when_a_built_billable_is_not_priced(self):
        # built 2 ALBs + EC2, priced only compute -> the ALBs are missing (the exact run11 shape)
        t = _transcript(
            ("mcp__aws-mcp__aws___run_script", "code", "operation_name='CreateLoadBalancer'"),
            ("mcp__aws-mcp__aws___run_script", "code", "operation_name='CreateLoadBalancer'"),
            ("Bash", "command", "aws ec2 run-instances --count 1"))
        rj = self._run_json([{"name": "compute"}, {"name": "storage"}], t)
        try:
            v = audit_run(rj)
        finally:
            os.unlink(t)
            os.unlink(rj)
        self.assertTrue(v["underpriced"])
        self.assertIn("load_balancer", v["missing"])

    def test_ok_when_everything_built_is_priced(self):
        t = _transcript(("mcp__aws-mcp__aws___run_script", "code", "operation_name='CreateLoadBalancer'"),
                        ("Bash", "command", "aws ec2 run-instances --count 1"))
        rj = self._run_json([{"name": "load_balancer"}, {"name": "compute"}, {"name": "storage"}], t)
        try:
            v = audit_run(rj)
        finally:
            os.unlink(t)
            os.unlink(rj)
        self.assertFalse(v["underpriced"])
        self.assertEqual(v["missing"], [])


if __name__ == "__main__":
    unittest.main()
