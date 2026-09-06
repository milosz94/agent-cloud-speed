"""A live describe may RESCUE a resource the create call could not identify. It may never spoil one.

Three consecutive live runs failed in three different ways, and all three were the same design flaw:
`describe_live` reads the RUNNING resource, a running resource answers with everything it knows, and all
of it was merged into the selector set before any pricing was attempted. Every service has a different
describe shape, so every new service was a new surprise -- the opposite of universal.

  run22: a container-service DEPLOYMENT borrowed its parent's Power and priced as another container.
  run23: an RDS instance arrived with its maintenance windows, status and CA certificate, and 864 SKUs
         from unrelated services matched one word each.
  live matrix: a SECURITY GROUP, which is free, came back ambiguous across 99 SKUs on its own group id,
         vpc id and description, making a run where all eight billable lines priced correctly report
         ok:false.

The create call is the authoritative record of what was ASKED FOR and is sufficient for most resources.
The describe is a fallback for a genuine vocabulary gap, of which exactly one is measured: Lightsail's
`relationalDatabaseBundleId: micro_2_0` appears in no published Lightsail SKU.
"""
import unittest
from unittest import mock

from acspeed.adapters import aws_ct_cost as ct


class TestTheCreateCallIsAskedFirst(unittest.TestCase):

    def setUp(self):
        self.calls = []

        def fake(resource, sel, region, profile=None):
            self.calls.append(dict(sel))
            return self.answers.pop(0)

        self.patch = mock.patch.object(ct, "_price_with_selectors", side_effect=fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    _RES = {"kind": "dbinstance", "event": "CreateDBInstance", "region": "us-east-1", "src": "rds",
            "params": {"dBInstanceClass": "db.t3.micro", "engine": "postgres"}}
    _LIVE = {"status": "available", "caCertificateIdentifier": "rds-ca-rsa2048-g1",
             "preferredMaintenanceWindow": "fri:09:37-fri:10:07"}

    def test_a_resolvable_resource_never_consults_the_live_describe(self):
        self.answers = [{"priced": True, "usagetype": "InstanceUsage:db.t3.micro"}]
        got = ct.price_resource_universal(self._RES, describe=lambda r: self._LIVE)
        self.assertTrue(got["priced"])
        self.assertEqual(len(self.calls), 1, "the describe must not be consulted at all")
        self.assertNotIn("status", self.calls[0])
        self.assertEqual(got["evidence"], "create call")

    def test_the_describe_rescues_what_the_create_call_could_not_identify(self):
        """Lightsail's bundle id: the one measured vocabulary gap."""
        self.answers = [{"priced": False, "reason": "no SKU carries ['micro_2_0']"},
                        {"priced": True, "usagetype": "USE1-DatabaseUsage:1GB"}]
        got = ct.price_resource_universal(self._RES, describe=lambda r: self._LIVE)
        self.assertTrue(got["priced"])
        self.assertEqual(len(self.calls), 2)
        self.assertIn("status", self.calls[1], "the second attempt carries the live properties")
        self.assertEqual(got["evidence"], "create call + live describe")

    def test_when_nothing_prices_the_create_calls_verdict_stands(self):
        """A FREE resource's create call always fails, so the describe always runs on exactly the
        resources that have no price. Its ambiguity must not become the reported failure: a security
        group ambiguous across 99 SKUs BLOCKS the run, while "no SKU carries" does not."""
        self.answers = [{"priced": False, "reason": "no SKU carries ['acspeed live test']"},
                        {"priced": False, "reason": "ambiguous: 99 on-demand SKUs match [...]",
                         "from_own_service": True}]
        got = ct.price_resource_universal({**self._RES, "kind": "securitygroup"},
                                          describe=lambda r: self._LIVE)
        self.assertFalse(got["priced"])
        self.assertTrue(got["reason"].startswith("no SKU carries"),
                        f"the describe's ambiguity leaked into the verdict: {got['reason']}")

    def test_a_describe_that_raises_is_not_a_price(self):
        def boom(_r):
            raise RuntimeError("cloud control said no")
        self.answers = [{"priced": False, "reason": "no SKU carries ['x']"}]
        got = ct.price_resource_universal(self._RES, describe=boom)
        self.assertFalse(got["priced"])
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
