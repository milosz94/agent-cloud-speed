"""The audit's VERDICT logic: the four ways it used to pass a run it should have failed.

Each test here reproduces a shape measured in aws-medium-b on 2026-09-05, where 7 of 19 runs were
discarded for silent under-pricing. The audit existed at the time and reported only 4 of the 7 as
UNDER-PRICED. The other three passed for three different reasons, all of them defects in the verdict
rather than in the measurement:

  * run10 / run19: a COUNT shortfall (2 or 3 Lightsail container services built, 1 priced) was printed
    next to the verdict "ok", because a shortfall was "reported but not failing".
  * run17: the agent called boto3 through a helper, ``go("lightsail","CreateContainerService")``, so the
    pattern anchored on ``operation_name='X'`` matched nothing in all 47 of its tool calls. The audit
    reported ``built={}`` and called that "ok" -- absence of evidence read as a pass.
  * aws-medium-a runs 7/9/10 were flagged for an unpriced EC2 the SUITE created, after the cost snapshot.
    Scanning the whole transcript instead of the deploy turn made three false accusations.
  * aws-easy run9 wrapped RunInstances in a try/except labelled "unexpected success" (a capability probe
    the agent expected to fail). The call counted, so a one-instance run looked like two.
"""
import json
import os
import tempfile
import unittest

from acspeed.aws_audit import audit_run, observed_identities


def _transcript(rows):
    """rows: (tool_name, field, text, timestamp|None, result_text|None)."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        for i, (name, field, text, ts, result) in enumerate(rows):
            use = {"message": {"content": [{"type": "tool_use", "name": name,
                                            "id": f"t{i}", "input": {field: text}}]}}
            if ts:
                use["timestamp"] = ts
            fh.write(json.dumps(use) + "\n")
            if result is not None:
                res = {"message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                "content": result}]}}
                if ts:
                    res["timestamp"] = ts
                fh.write(json.dumps(res) + "\n")
    return path


def _run_json(components, transcript, token="acs1", wall=600.0):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump({"run": 1, "run_token": token, "url": "http://x", "agent_wall_s": wall,
                   "cost_run_rate": {"components": components},
                   "deploy": {"transcript": transcript}}, fh)
    return path


T0 = "2026-09-05T01:00:00.000Z"
T_IN = "2026-09-05T01:05:00.000Z"       # inside a 600 s deploy turn
T_SUITE = "2026-09-05T02:30:00.000Z"    # long after it: the suite


class TestCountShortfallFails(unittest.TestCase):
    """run10 / run19: two container services served, one priced, verdict "ok"."""

    def test_two_built_one_priced_is_underpriced(self):
        t = _transcript([
            ("mcp__aws-mcp__aws___run_script", "code", 'operation_name="CreateContainerService"', T0,
             '{"url":"https://umami-acs1.aa.us-east-1.cs.amazonlightsail.com/","state":"RUNNING"}'),
            ("mcp__aws-mcp__aws___run_script", "code", 'operation_name="CreateContainerService"', T_IN,
             '{"url":"https://alcove-acs1.bb.us-east-2.cs.amazonlightsail.com/","state":"RUNNING"}'),
        ])
        rj = _run_json([{"name": "compute:lightsail-container"}], t)
        try:
            r = audit_run(rj)
        finally:
            os.unlink(t), os.unlink(rj)
        self.assertEqual(r["count_shortfall"], {"lightsail": (2, 1)})
        self.assertTrue(r["underpriced"], "a count shortfall must FAIL, not be a note next to 'ok'")


class TestBareStringOperationName(unittest.TestCase):
    """run17: ``go("lightsail","CreateContainerService")`` matched nothing, so built={} and verdict "ok"."""

    def test_helper_style_boto3_call_is_still_counted(self):
        t = _transcript([
            ("mcp__aws-mcp__aws___run_script", "code",
             'out = await go("lightsail","CreateContainerService", params=p)', T0,
             '{"url":"https://umami-acs1.aa.us-east-2.cs.amazonlightsail.com/","state":"RUNNING"}'),
            ("mcp__aws-mcp__aws___run_script", "code", 'await go("rds","CreateDBInstance", params=q)', T_IN,
             '{"Endpoint":{"Address":"umami-db-acs1.cc.us-east-1.rds.amazonaws.com"}}'),
        ])
        rj = _run_json([{"name": "compute:lightsail-container"}], t)
        try:
            r = audit_run(rj)
        finally:
            os.unlink(t), os.unlink(rj)
        self.assertEqual(r["built"].get("lightsail"), 1)
        self.assertEqual(r["built"].get("rds"), 1)
        self.assertIn("rds", r["missing"])
        self.assertTrue(r["underpriced"])


class TestEmptyBuiltIsUnverifiableNotOk(unittest.TestCase):
    """Absence of evidence is a statement about the SCANNER, never a pass for the run."""

    def test_no_recognised_create_is_unverifiable(self):
        t = _transcript([("Bash", "command", "python3 /app/deploy_everything.py", T0, "done")])
        rj = _run_json([{"name": "compute"}], t)
        try:
            r = audit_run(rj)
        finally:
            os.unlink(t), os.unlink(rj)
        self.assertEqual(r["built"], {})
        self.assertTrue(r["unverifiable"], "built={} must be UNVERIFIABLE, never 'ok'")


class TestDeployTurnCut(unittest.TestCase):
    """The snapshot is taken when the deploy turn ends, so a create the SUITE made is not the price's fault."""

    def test_a_create_after_the_deploy_turn_is_not_counted(self):
        t = _transcript([
            ("Bash", "command", "aws ec2 run-instances --count 1", T0, "i-0aaaaaaaaaaaaaaa1 acs1"),
            ("Bash", "command", "aws elbv2 create-load-balancer --name site-b", T_SUITE,
             "sb-acs1-999.us-east-1.elb.amazonaws.com"),
        ])
        rj = _run_json([{"name": "compute"}, {"name": "storage"}], t)
        try:
            r = audit_run(rj)
        finally:
            os.unlink(t), os.unlink(rj)
        self.assertEqual(r["built"].get("load_balancer"), None,
                         "the suite's site-B ALB post-dates the cost snapshot and must not be counted")
        self.assertFalse(r["underpriced"])


class TestProbeIsNotAProvision(unittest.TestCase):
    """aws-easy run9: a RunInstances probe caught by an in-script try/except is not a second instance."""

    def test_shortfall_needs_corroborating_identities(self):
        t = _transcript([
            ("mcp__aws-mcp__aws___run_script", "code",
             'try:\n await call_boto3(operation_name="RunInstances")\n out["with_profile"]="unexpected success"',
             T0, '{"with_profile":"An error occurred (UnauthorizedOperation)"}'),
            ("mcp__aws-mcp__aws___run_script", "code", 'await call_boto3(operation_name="RunInstances")', T_IN,
             '{"Instances":[{"InstanceId":"i-0bbbbbbbbbbbbbbb2"}],"Name":"umami-acs1"}'),
        ])
        rj = _run_json([{"name": "compute"}, {"name": "storage"}], t)
        try:
            ident = observed_identities(t, "acs1", 600.0)
            r = audit_run(rj)
        finally:
            os.unlink(t), os.unlink(rj)
        self.assertEqual(len(ident["ec2"]), 1, "only ONE instance id was ever returned")
        self.assertEqual(r["built"].get("ec2"), 2)               # two calls counted
        self.assertFalse(r["underpriced"],
                         "one observed instance against one priced compute is not a shortfall")


class TestIdentifiersAreCountedFromPlainText(unittest.TestCase):
    """ONE load balancer named twice must count as ONE, whatever its position on the line.

    The first cut of this read the result via ``json.dumps``, which renders a newline as the two
    characters backslash + n. ``\\b`` then matches between them and the capture eats the 'n', so the
    SAME host yields ``umami-x-1`` when it appears mid-line and ``numami-x-1`` when it starts a line.
    Both land in the identity set, so one resource counts as two. That is what made aws-medium-b run08
    report three load balancers from two. The whole shortfall verdict rests on these counts, so an
    inflated count is a false under-price accusation."""

    def test_the_same_host_at_line_start_and_mid_line_is_one_resource(self):
        host = "umami-acs1-2103432357.us-east-1.elb.amazonaws.com"
        body = f"created lb {host} ok\n{host}\n"          # same host, mid-line then line-start
        t = _transcript([("Bash", "command", "aws elbv2 create-load-balancer --name x", T0, body)])
        try:
            ident = observed_identities(t, "acs1", 600.0)
        finally:
            os.unlink(t)
        self.assertEqual(ident["load_balancer"], {"umami-acs1-2103432357"},
                         f"one host must yield one identity, got {ident['load_balancer']}")


if __name__ == "__main__":
    unittest.main()
