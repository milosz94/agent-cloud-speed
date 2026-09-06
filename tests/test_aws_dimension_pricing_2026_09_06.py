"""Some services are billed PER UNIT of a dimension, not per sized SKU.

Fargate and App Runner charge per vCPU-hour and per GB-hour, so the create call holds the QUANTITY and
the disclosure holds the RATE. Measured on the live arch matrix before this existed: a Fargate task --
the architecture agents chose 10/10 times in medium-a -- priced at ZERO, and App Runner's CreateService
reduced to no selectors at all because its size is nested inside `InstanceConfiguration`.

Nothing about either service is written down here. The published CFN schema says which fields carry a
size and what unit they are in; the published price list says which SKU is billed per that unit; and the
unqualified usagetype is the default configuration, the same reasoning that pins `tenancy: Shared`.
"""
import unittest
from unittest import mock

from acspeed.adapters import aws_ct_cost as ct


class TestTheSchemaDeclaresTheUnit(unittest.TestCase):
    """AWS states the ratio two ways, and both are machine-readable."""

    def test_a_pattern_listing_raw_beside_human_values(self):
        got = ct._unit_of({"pattern": "256|512|1024|2048|4096|(0.25|0.5|1|2|4) vCPU"})
        self.assertEqual(got, ("vCPU", 1024.0))

    def test_prose_is_accepted_only_when_two_examples_agree(self):
        """A single example could be a misparse; two that agree cannot both be misparsed the same way."""
        agree = ("Supported values are between ``128`` CPU units (``0.125`` vCPUs) and ``196608`` "
                 "CPU units (``192`` vCPUs).")
        self.assertEqual(ct._unit_of({"description": agree}), ("vCPU", 1024.0))
        disagree = ("between ``128`` CPU units (``0.125`` vCPUs) and ``196608`` CPU units "
                    "(``999`` vCPUs).")
        self.assertIsNone(ct._unit_of({"description": disagree}),
                          "contradicting pairs must be refused, not averaged")

    def test_a_declared_memory_unit(self):
        self.assertEqual(ct._unit_of({"description": "The amount (in MiB) of memory used by the task."}),
                         ("GB", 1024.0))

    def test_a_field_declaring_nothing_yields_nothing(self):
        """Then the resource is reported unpriced. A guessed ratio is a silent wrong number."""
        self.assertIsNone(ct._unit_of({"description": "The number of things.", "type": "integer"}))
        self.assertIsNone(ct._unit_of({}))


class TestTheQuantityComesFromTheCreateCall(unittest.TestCase):

    def test_a_nested_field_is_found_by_its_own_name(self):
        """A create call and a CFN type agree on what a field is CALLED, not on where it sits."""
        params = {"sourceConfiguration": {}, "instanceConfiguration": {"cpu": "256", "memory": "512"}}
        self.assertEqual(ct._value_at(params, "InstanceConfiguration.Cpu"), 256.0)
        self.assertEqual(ct._value_at(params, "InstanceConfiguration.Memory"), 512.0)

    def test_a_non_numeric_field_yields_no_part(self):
        """`ScalableDimension` parses as a GB field from its description but holds
        "ecs:service:DesiredCount", so requiring a positive NUMBER neutralises the misparse."""
        self.assertIsNone(ct._value_at({"scalableDimension": "ecs:service:DesiredCount"},
                                       "ScalableDimension"))
        self.assertIsNone(ct._value_at({"cpu": 0}, "Cpu"))


class TestTheDisclosureNamesTheRate(unittest.TestCase):

    _ECS = {"products": {
        "BASE": {"attributes": {"usagetype": "USE1-Fargate-vCPU-Hours:perCPU", "cputype": "perCPU",
                                "locationType": "AWS Region"}},
        "ARM":  {"attributes": {"usagetype": "USE1-Fargate-ARM-vCPU-Hours:perCPU", "cputype": "perCPU",
                                "locationType": "AWS Region"}},
        "WIN":  {"attributes": {"usagetype": "USE1-Fargate-Windows-OS-Hours:perCPU",
                                "cputype": "perCPU OS License Fee", "locationType": "AWS Region"}}},
        "terms": {"OnDemand": {
            "BASE": {"t": {"priceDimensions": {"d": {"unit": "hours",
                                                     "pricePerUnit": {"USD": "0.04048"}}}}},
            "ARM":  {"t": {"priceDimensions": {"d": {"unit": "hours",
                                                     "pricePerUnit": {"USD": "0.03238"}}}}},
            "WIN":  {"t": {"priceDimensions": {"d": {"unit": "hours",
                                                     "pricePerUnit": {"USD": "0.046552"}}}}}}}}

    def test_the_unqualified_line_is_the_default_configuration(self):
        """A qualifier lengthens the name. Every qualified variant measured is CHEAPER, so picking one
        would UNDER-count -- this rule fails toward over-counting, which is the safe direction."""
        px = "acspeed.adapters.aws_price_index"
        with mock.patch(f"{px}.service_for", return_value=["AmazonECS"]), \
             mock.patch(f"{px}.region_offer", return_value=self._ECS):
            got = ct.price_dimension("ecs", "us-east-1", "vCPU", ["FARGATE"])
        self.assertTrue(got["priced"], got)
        self.assertEqual(got["usagetype"], "USE1-Fargate-vCPU-Hours:perCPU")
        self.assertAlmostEqual(got["hourly_usd"], 0.04048)

    def test_words_that_name_nothing_do_not_veto_the_rate(self):
        """A task definition's words are its family NAME, its network mode and its raw numbers; the word
        that places it, FARGATE, sits in a LIST the selector rule does not read. Requiring them left
        Fargate unpriced on a live run."""
        px = "acspeed.adapters.aws_price_index"
        with mock.patch(f"{px}.service_for", return_value=["AmazonECS"]), \
             mock.patch(f"{px}.region_offer", return_value=self._ECS):
            got = ct.price_dimension("ecs", "us-east-1", "vCPU",
                                     ["acs-run-token", "awsvpc", "256", "512"])
        self.assertTrue(got["priced"], got)
        self.assertEqual(got["usagetype"], "USE1-Fargate-vCPU-Hours:perCPU")

    def test_a_service_with_no_such_dimension_is_refused(self):
        px = "acspeed.adapters.aws_price_index"
        with mock.patch(f"{px}.service_for", return_value=["AmazonECS"]), \
             mock.patch(f"{px}.region_offer", return_value=self._ECS):
            got = ct.price_dimension("ecs", "us-east-1", "IOPS", [])
        self.assertFalse(got["priced"])
        self.assertIn("no published per-IOPS rate", got["reason"])


class TestOneBadSchemaCannotSilenceTheRest(unittest.TestCase):
    """`aws-budgets-budget` contains a self-referencing $ref. The walk recursed to RecursionError and a
    single outer try/except turned that into ZERO size fields for EVERY service, so Fargate and App
    Runner were silently unpriced with no error anywhere."""

    def test_a_self_referencing_definition_terminates(self):
        doc_defs = {"Node": {"properties": {"child": {"$ref": "#/definitions/Node"},
                                            "Size": {"description": "The amount (in MiB) of memory."}}}}
        seen = []

        def scan(props, prefix="", been=()):
            for k, v in (props or {}).items():
                ref = v.get("$ref") if isinstance(v.get("$ref"), str) else None
                if ref:
                    if ref in been:
                        continue
                    spec, here = doc_defs.get(ref.split("/")[-1], {}), been + (ref,)
                else:
                    spec, here = v, been
                if ct._unit_of(spec):
                    seen.append(prefix + k)
                if spec.get("properties") and len(here) < 12:
                    scan(spec["properties"], prefix + k + ".", here)

        scan({"root": {"$ref": "#/definitions/Node"}})
        self.assertEqual(seen, ["root.Size"], "the cycle must terminate and still find the real field")


if __name__ == "__main__":
    unittest.main()


class TestHowManyOfItAreRunning(unittest.TestCase):
    """A task definition is billed not at all; each TASK started from it is. Pricing the definition once
    prices a three-task deployment as one, and nothing else catches that: conformance verifies that every
    billed usagetype was produced, not that its quantity is right."""

    def _discover(self, events):
        import json as _json
        return ct.discover("2026-09-06T12:00:00Z", "2026-09-06T15:00:00Z", "acs22", ["us-east-1"])

    def _events(self, n_tasks):
        import json as _json
        evs = [{"EventName": "RegisterTaskDefinition", "CloudTrailEvent": _json.dumps({
            "eventTime": "2026-09-06T13:00:00Z", "eventSource": "ecs.amazonaws.com",
            "requestParameters": {"family": "acs22", "cpu": "256", "memory": "512",
                                  "requiresCompatibilities": ["FARGATE"]}, "responseElements": {}})}]
        for i in range(n_tasks):
            evs.append({"EventName": "RunTask", "CloudTrailEvent": _json.dumps({
                "eventTime": f"2026-09-06T13:0{i+1}:00Z", "eventSource": "ecs.amazonaws.com",
                "requestParameters": {"cluster": "acs22", "launchType": "FARGATE",
                                      "taskDefinition": "arn:aws:ecs:us-east-1:1:task-definition/acs22:1"},
                "responseElements": {}})})
        return evs

    def _quantities(self, n_tasks):
        with mock.patch.object(ct, "_aws", side_effect=[({"Events": self._events(n_tasks)}, None)]):
            found, _u = self._discover(None)
        td = [r for r in found if r["event"] == "RegisterTaskDefinition"][0]
        return {p["part"]: p["quantity"] for p in ct.billable_parts(td) if p.get("dimension")}

    def test_three_tasks_are_three(self):
        self.assertEqual(self._quantities(3), {"vcpu:Cpu": 0.75, "gb:Memory": 1.5})

    def test_one_task_is_unchanged(self):
        self.assertEqual(self._quantities(1), {"vcpu:Cpu": 0.25, "gb:Memory": 0.5})

    def test_no_task_still_prices_the_definition_once(self):
        """A failed match must leave the count at one -- today's behaviour -- not at zero."""
        self.assertEqual(self._quantities(0), {"vcpu:Cpu": 0.25, "gb:Memory": 0.5})

    def test_a_resource_merely_sharing_the_naming_convention_is_not_an_instance(self):
        """The ECS cluster is also called `acs22`; only a parameter NAMED after the referenced noun
        counts, so the cluster is not mistaken for a task."""
        import json as _json
        evs = self._events(1) + [{"EventName": "CreateCluster", "CloudTrailEvent": _json.dumps({
            "eventTime": "2026-09-06T12:59:00Z", "eventSource": "ecs.amazonaws.com",
            "requestParameters": {"clusterName": "acs22"}, "responseElements": {}})}]
        with mock.patch.object(ct, "_aws", side_effect=[({"Events": evs}, None)]):
            found, _u = ct.discover("2026-09-06T12:00:00Z", "2026-09-06T15:00:00Z", "acs22", ["us-east-1"])
        td = [r for r in found if r["event"] == "RegisterTaskDefinition"][0]
        self.assertEqual(td.get("instances"), 1)
