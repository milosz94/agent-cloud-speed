"""An identifier the price list has never heard of is resolvable, not unpriceable.

A create call names things in the vendor's CATALOGUE vocabulary; the price list is keyed on
SPECIFICATIONS. Measured on a Lightsail database: the create says ``relationalDatabaseBundleId:
micro_2_0``, that string appears in none of the 150 published Lightsail products in us-east-1, and the
resource cannot supply the specification either -- its Cloud Control schema exposes the bundle id and
nothing about the hardware. Before this, the run reported a real billable resource as unpriceable and
the only thing that ever priced it was a hand-written ``aws lightsail get-relational-database-bundles``
call living in a scratch harness, i.e. exactly the per-service patch this module exists to avoid.
"""
import unittest
from unittest import mock

from acspeed.adapters import aws_ct_cost as ct


class TestTheCatalogueIsFoundNotNamed(unittest.TestCase):

    def _run(self, ops, responses, param="relationalDatabaseBundleId", value="micro_2_0"):
        calls = []

        def fake_aws(args, timeout=120):
            calls.append(args[1])
            return responses.get(args[1]), None

        ct._CATALOG_MEM.clear()
        with mock.patch.object(ct, "_catalog_ops", return_value=("lightsail", ops)), \
             mock.patch.object(ct, "_aws", side_effect=fake_aws):
            got = ct.resolve_catalog_id("lightsail", param, value, "us-east-1")
        return got, calls

    def test_the_record_carrying_the_identifier_becomes_the_selectors(self):
        got, _calls = self._run(
            ["GetRelationalDatabaseBundles"],
            {"get-relational-database-bundles": {"bundles": [
                {"bundleId": "small_2_0", "ramSizeInGb": 2.0, "diskSizeInGb": 80},
                {"bundleId": "micro_2_0", "name": "Micro", "ramSizeInGb": 1.0, "diskSizeInGb": 40,
                 "isActive": True}]}})
        self.assertEqual(got.get("ramSizeInGb"), 1.0)
        self.assertEqual(got.get("diskSizeInGb"), 40)
        self.assertNotIn("isActive", got, "a boolean is not a specification")
        self.assertNotIn("name", got,
                         "the bundle's name is 'Micro', which is also a container size: taking a LABEL "
                         "as a selector priced a database as a container")
        self.assertNotIn("bundleId", got, "the identifier being resolved cannot be its own answer")

    def test_operations_are_tried_in_order_of_shared_words_with_the_parameter(self):
        """The vendor named the parameter and its catalogue after the same thing. Ordering by that is
        what turns 31 candidate operations into two calls."""
        ops = ["GetAlarms", "GetOperations", "GetRelationalDatabaseBundles", "GetBuckets"]
        _got, calls = self._run(ops, {"get-relational-database-bundles": {"bundles": [
            {"bundleId": "micro_2_0", "ramSizeInGb": 1.0}]}})
        self.assertEqual(calls[0], "get-relational-database-bundles",
                         f"ranked wrong; tried {calls}")

    def test_it_stops_instead_of_calling_every_unrelated_operation(self):
        """An identifier no catalogue contains must cost a bounded number of calls, not the whole API."""
        ops = ["GetAlarms", "GetBuckets", "GetOperations", "GetDistributions", "GetDomains"]
        got, calls = self._run(ops, {})
        self.assertEqual(got, {})
        self.assertEqual(calls, [], "operations sharing NO word with the parameter must not be called")

    def test_nothing_is_invented_when_the_catalogue_does_not_have_it(self):
        got, _calls = self._run(
            ["GetRelationalDatabaseBundles"],
            {"get-relational-database-bundles": {"bundles": [{"bundleId": "small_2_0",
                                                              "ramSizeInGb": 2.0}]}})
        self.assertEqual(got, {}, "a near miss is not the answer")


class TestNumbersAreSpelledTheWayTheServiceSpellsThem(unittest.TestCase):

    def test_the_field_declares_its_own_unit(self):
        self.assertEqual(ct._spellings("ramSizeInGb", 1.0, {"gb", "tb"}), ["1gb"])
        self.assertEqual(ct._spellings("diskSizeInGb", 40, {"gb"}), ["40gb"])

    def test_a_count_is_not_gigabytes(self):
        """`cpuCount: 2` spelled as "2GB" is a real memory value, so a DATABASE matched a container SKU
        and was priced at $9.81 instead of $14.72. A field that declares no unit offers none."""
        self.assertEqual(ct._spellings("cpuCount", 2, {"gb", "tb"}), [])
        self.assertEqual(ct._spellings("price", 15.0, {"gb"}), [])

    def test_a_unit_the_price_list_never_writes_is_not_used(self):
        self.assertEqual(ct._spellings("memorySizeInMb", 512, {"gb", "tb"}), [])

    def test_a_bare_number_is_never_offered(self):
        """The module's oldest measured rule: a number is a QUANTITY and matches attributes
        spuriously (EC2 value `1` matches 13 different attributes)."""
        self.assertNotIn("1", ct._spellings("ramSizeInGb", 1.0, {"gb"}))
        self.assertEqual(ct._spellings("ramSizeInGb", 1.0, set()), [])

    def test_a_boolean_is_not_a_specification(self):
        self.assertEqual(ct._spellings("isActive", True, {"gb"}), [])


class TestResourceIdsAreExcludedByShapeNotByList(unittest.TestCase):
    """The previous rule listed sg-/subnet-/vpc-/ami-/i-/eni-/rtb-/igw- by hand and kept exactly
    `bundleid`/`blueprintid`: one vendor list to exclude ids, another to re-admit two of them."""

    def _sel(self, params):
        return ct.selectors_for({"params": params})

    def test_a_prefix_nobody_listed_is_still_a_resource_id(self):
        got = self._sel({"natGatewayId": "nat-0123456789abcdef0",
                         "transitGatewayId": "tgw-0a1b2c3d4e5f60718"})
        self.assertEqual(got, {}, f"unlisted id prefixes leaked in as selectors: {got}")

    def test_a_catalogue_id_survives(self):
        got = self._sel({"relationalDatabaseBundleId": "micro_2_0",
                         "relationalDatabaseBlueprintId": "postgres_16",
                         "relationalDatabaseName": "umamidb-acscdb8a4d2",
                         "availabilityZone": "us-east-1a",
                         "clientToken": "3d63a1fd-777b-4f6e-a324-382dafdb2f25"})
        self.assertEqual(got, {"relationalDatabaseBundleId": "micro_2_0",
                               "relationalDatabaseBlueprintId": "postgres_16"})


class TestTheSuiteCannotCorruptTheRealPriceCache(unittest.TestCase):
    """The defect that cost two live container services their price: the suite patches `region_offer`
    with a fixture, a summary derived from it was written into the shared cache under the REAL service's
    name, and the next production run read the fixture back as the published disclosure."""

    def test_the_cache_under_test_is_not_the_users(self):
        from acspeed.adapters import aws_price_index as px
        import os
        real = os.path.expanduser("~/.cache/acspeed-pricing")
        self.assertNotEqual(os.path.abspath(px._CACHE), os.path.abspath(real),
                            "the test suite is writing into the cache that real runs read")

    def test_a_digest_built_from_a_fixture_lands_in_the_test_cache(self):
        from acspeed.adapters import aws_price_index as px
        import os
        px._DIGEST_MEM.clear()
        offer = {"products": {"S": {"attributes": {"memory": "1GB", "power": "Nano",
                                                   "usagetype": "USE1-ContainerSvcUsage:Nano"}}}}
        with mock.patch.object(px, "region_offer", return_value=offer):
            uni = px.value_universe("AmazonLightsail", "us-east-1")
        px._DIGEST_MEM.clear()
        self.assertIn("nano", uni)
        written = [f for f in os.listdir(px._CACHE) if f.startswith("digest_")]
        self.assertTrue(written, "the digest should still be cached, just not where production reads")


if __name__ == "__main__":
    unittest.main()
