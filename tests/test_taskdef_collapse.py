"""Regression: ECS task-definition REVISIONS must not each bill as a running task.

Measured 2026-09-07 on aws-medium-a: priced Fargate vCPU parts equalled task definitions registered,
exactly (7, 4, 4, 2), against 1, 0, 0 and 0 standalone tasks. run08 was charged $192.08/mo of vCPU for
one running task.
"""
import unittest

from acspeed.adapters.aws_ct_cost import collapse_task_definitions


def td(family, at, cpu="1024", mem="2048"):
    return {"kind": "taskdefinition", "at": at, "identity": f"{family}:{at}",
            "params": {"family": family, "cpu": cpu, "memory": mem}}


def svc(family, desired="1"):
    return {"kind": "service", "at": "9", "identity": "svc",
            "params": {"taskDefinition": f"arn:aws:ecs:us-east-1:1:task-definition/{family}:1",
                       "desiredCount": desired}}


def run_task(family, count="1"):
    return {"kind": "task", "at": "9", "identity": "task",
            "params": {"taskDefinition": f"arn:aws:ecs:us-east-1:1:task-definition/{family}:1",
                       "count": count}}


class TestCollapse(unittest.TestCase):
    def test_revisions_of_one_family_collapse_to_one_bundle(self):
        res = [td("app", str(i)) for i in range(1, 8)] + [run_task("app")]
        out, notes = collapse_task_definitions(res)
        tds = [r for r in out if r["kind"] == "taskdefinition"]
        self.assertEqual(len(tds), 1)                 # 7 revisions -> 1, the run08 case
        self.assertEqual(tds[0]["at"], "7")           # and it is the LATEST revision
        self.assertEqual(tds[0]["runner_count"], 1)
        self.assertTrue(any("7 revision" in n for n in notes))

    def test_two_families_stay_two_bundles(self):
        res = [td("app", "1"), td("app", "2"), td("migrate", "1"), run_task("migrate")]
        out, _ = collapse_task_definitions(res)
        fams = {r["params"]["family"] for r in out if r["kind"] == "taskdefinition"}
        self.assertEqual(fams, {"app", "migrate"})

    def test_runner_count_multiplies(self):
        out, _ = collapse_task_definitions([td("app", "1"), svc("app", desired="3")])
        self.assertEqual([r for r in out if r["kind"] == "taskdefinition"][0]["runner_count"], 3)

    def test_family_with_no_recorded_runner_still_bills_once(self):
        # NOT dropped: run08 served from a family with no CreateService/RunTask in the window, so
        # dropping runner-less families would turn an over-count into a silent under-count.
        out, _ = collapse_task_definitions([td("app", "1"), td("app", "2")])
        tds = [r for r in out if r["kind"] == "taskdefinition"]
        self.assertEqual(len(tds), 1)
        self.assertEqual(tds[0]["runner_count"], 1)

    def test_no_task_definitions_is_a_passthrough(self):
        res = [{"kind": "dbinstance", "at": "1", "identity": "db", "params": {}}]
        out, notes = collapse_task_definitions(res)
        self.assertEqual(out, res)
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()


class TestRunnerCount(unittest.TestCase):
    """aws-easy run03: 1 service (desiredCount 1) + 3 sequential RunTask retries summed to 4 runners
    and priced 16 vCPU, $621.65/mo against a published $66.45. Retries are sequential, not concurrent."""

    def test_retries_do_not_multiply_a_service(self):
        res = [td("app", "1"), svc("app", desired="1"),
               run_task("app"), run_task("app"), run_task("app")]
        out, _ = collapse_task_definitions(res)
        self.assertEqual([r for r in out if r["kind"] == "taskdefinition"][0]["runner_count"], 1)

    def test_repeated_task_attempts_take_the_max_not_the_sum(self):
        res = [td("app", "1"), run_task("app", "1"), run_task("app", "1"), run_task("app", "2")]
        out, _ = collapse_task_definitions(res)
        self.assertEqual([r for r in out if r["kind"] == "taskdefinition"][0]["runner_count"], 2)

    def test_a_service_updated_twice_is_still_one_service(self):
        res = [td("app", "1"), svc("app", desired="1"), svc("app", desired="2")]
        out, _ = collapse_task_definitions(res)   # same serviceName -> latest desiredCount wins
        self.assertEqual([r for r in out if r["kind"] == "taskdefinition"][0]["runner_count"], 2)

    def test_two_distinct_services_on_one_family_do_add(self):
        a = svc("app", desired="1"); a["params"]["serviceName"] = "svc-a"
        b = svc("app", desired="2"); b["params"]["serviceName"] = "svc-b"
        out, _ = collapse_task_definitions([td("app", "1"), a, b])
        self.assertEqual([r for r in out if r["kind"] == "taskdefinition"][0]["runner_count"], 3)

    def test_revision_multiplicity_is_not_applied_twice(self):
        # discover marks identical creates with `instances`; for a task definition that IS the revision
        # count, and billable_parts multiplies the part quantity by it. aws-easy run03: 4 resources at
        # instances=4 priced 16 vCPU, $621.65/mo against a published $66.45.
        res = [dict(td("app", str(i)), instances=4) for i in range(1, 5)] + [svc("app", desired="1")]
        out, _ = collapse_task_definitions(res)
        bundle = [r for r in out if r["kind"] == "taskdefinition"][0]
        self.assertEqual(bundle["instances"], 1)
        self.assertEqual(bundle["runner_count"], 1)
