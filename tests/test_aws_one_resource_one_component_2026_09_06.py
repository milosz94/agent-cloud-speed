"""Several creates can name the SAME resource, and counting them separately invents infrastructure.

Measured on run22 (2026-09-06, the first LIVE run after the catalogue hop shipped): six
``CreateContainerServiceDeployment`` events, each of which deploys a new version of an EXISTING
container service, were priced as six additional containers. $64.77/mo of infrastructure that never
existed, on a run whose true standing rate is $36.30.

⛔ OFFLINE RE-PRICING COULD NOT HAVE CAUGHT IT. A deployment carries no size of its own; what lends it
one is `describe_live` asking Cloud Control about the still-running parent, which matched
AWS::Lightsail::Container because "container" is a substring of "containerservicedeployment" and
returned the SERVICE's `Power`. After teardown that call returns nothing and the double count silently
disappears, which is exactly what every re-pricing of a finished run showed. The lesson is not about
deployments: a defect that only exists while the resources are alive cannot be verified against a
torn-down run.
"""
import json
import unittest
from unittest import mock

from acspeed.adapters import aws_ct_cost as ct


def _event(name, params, when, src="lightsail"):
    return {"EventName": name, "CloudTrailEvent": json.dumps({
        "eventTime": when, "eventSource": f"{src}.amazonaws.com",
        "requestParameters": params, "responseElements": {}})}


class TestOneResourceOneComponent(unittest.TestCase):

    def _discover(self, events):
        with mock.patch.object(ct, "_aws", side_effect=[({"Events": events}, None)]):
            return ct.discover("2026-09-05T23:00:00Z", "2026-09-06T01:00:00Z", "acs22",
                               ["us-east-1"])

    def test_a_deployment_is_not_another_container_service(self):
        """run22's exact shape: two services, six deployments of them."""
        events = [_event("CreateContainerService", {"serviceName": "umami-acs22", "power": "small"},
                         "2026-09-05T23:27:48Z"),
                  _event("CreateContainerService", {"serviceName": "alcove-acs22", "power": "nano"},
                         "2026-09-05T23:27:49Z")]
        for i in range(3):
            for svc in ("umami-acs22", "alcove-acs22"):
                events.append(_event("CreateContainerServiceDeployment",
                                     {"serviceName": svc, "containers": {}},
                                     f"2026-09-06T00:0{i + 1}:0{i}Z"))
        found, _unclassified = self._discover(events)
        self.assertEqual(len(found), 2, [f["event"] for f in found])
        self.assertEqual({f["event"] for f in found}, {"CreateContainerService"})

    def test_the_create_is_kept_not_the_later_mutation(self):
        """The earliest create is the one that stood the resource up, and the one carrying its size."""
        found, _u = self._discover([
            _event("CreateContainerServiceDeployment", {"serviceName": "umami-acs22"},
                   "2026-09-06T00:05:00Z"),
            _event("CreateContainerService", {"serviceName": "umami-acs22", "power": "small"},
                   "2026-09-05T23:27:48Z")])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["event"], "CreateContainerService")
        self.assertEqual(found[0]["params"].get("power"), "small")

    def test_two_different_resources_are_still_two(self):
        """The guard must not merge distinct resources: this is an UNDER-count if it goes wrong."""
        found, _u = self._discover([
            _event("CreateContainerService", {"serviceName": "umami-acs22"}, "2026-09-05T23:27:48Z"),
            _event("CreateContainerService", {"serviceName": "alcove-acs22"}, "2026-09-05T23:27:49Z")])
        self.assertEqual(len(found), 2)

    def test_the_same_name_in_a_different_service_is_a_different_resource(self):
        found, _u = self._discover([
            _event("CreateContainerService", {"serviceName": "umami-acs22"}, "2026-09-05T23:27:48Z"),
            _event("CreateDBInstance", {"dBInstanceIdentifier": "umami-acs22"},
                   "2026-09-05T23:27:49Z", src="rds")])
        self.assertEqual(len(found), 2, "dedupe is scoped to one service")


class TestIdentityIsTheResourcesOwnName(unittest.TestCase):
    """Identity feeds deduplication, so picking a name that is not the resource's own risks merging two
    resources into one."""

    def test_the_inner_database_name_is_not_the_database(self):
        """`CreateRelationalDatabase` carries `relationalDatabaseName` (what was provisioned) AND
        `masterDatabaseName` ("umami", a database created inside it). An alphabetical tie-break between
        two *Name fields picked the second."""
        got = ct._identity({"relationalDatabaseName": "umamidb-acs22", "masterDatabaseName": "umami",
                            "relationalDatabaseBundleId": "micro_2_0"}, "CreateRelationalDatabase")
        self.assertEqual(got, "umamidb-acs22")

    def test_a_catalogue_id_is_never_the_identity(self):
        """Ranking a bare *Id first made `postgres_16` the database's identity, which would have merged
        two databases sharing an engine."""
        got = ct._identity({"relationalDatabaseBlueprintId": "postgres_16",
                            "relationalDatabaseName": "umamidb-acs22"}, "CreateRelationalDatabase")
        self.assertEqual(got, "umamidb-acs22")

    def test_an_identifier_beats_an_unrelated_name(self):
        self.assertEqual(ct._identity({"dBName": "umami", "dBInstanceIdentifier": "umami-db-x"},
                                      "CreateDBInstance"), "umami-db-x")

    def test_a_deployment_still_resolves_to_its_parents_identity(self):
        self.assertEqual(ct._identity({"serviceName": "umami-acs22", "containers": {}},
                                      "CreateContainerServiceDeployment"),
                         ct._identity({"serviceName": "umami-acs22", "power": "small"},
                                      "CreateContainerService"))


if __name__ == "__main__":
    unittest.main()
