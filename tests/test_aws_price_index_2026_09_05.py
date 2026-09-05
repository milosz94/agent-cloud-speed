"""Pricing from the PUBLISHED disclosure: the two ways this returned a WRONG price, not no price.

AWS must publish the price of everything it bills, so "unpriceable" is never a property of a resource.
But a lookup that returns the WRONG published price is worse than one that returns none, and both of
these did exactly that before being fixed.
"""
import unittest
from unittest import mock

from acspeed.adapters import aws_price_index as px


def _offer(products):
    """A minimal offer file: {sku: (attributes, usd_per_hour)}."""
    prods, terms = {}, {}
    for sku, (attrs, usd) in products.items():
        prods[sku] = {"attributes": attrs}
        terms[sku] = {f"{sku}.T": {"priceDimensions": {
            f"{sku}.T.D": {"unit": "Hrs", "pricePerUnit": {"USD": str(usd)},
                           "description": attrs.get("usagetype", "")}}}}
    return {"products": prods, "terms": {"OnDemand": terms}}


# The real shape measured on EC2 us-east-1: instanceType=t3.medium has SIXTY SKUs and only ONE of them
# is the public on-demand line. The naive first match took UnusedBox at $0.06/hr against the true
# $0.0416/hr, a 46% overcharge reported with full confidence.
_EC2 = _offer({
    # UnusedBox FIRST, as the real offer file orders it: a fixture that lists the correct SKU first makes
    # the "not the first match" assertion pass without testing anything.
    "SKU_UNUSED": ({"instanceType": "t3.medium", "usagetype": "UnusedBox:t3.medium", "tenancy": "Shared",
                    "operatingSystem": "Linux", "capacitystatus": "UnusedCapacityReservation",
                    "preInstalledSw": "NA", "licenseModel": "No License required"}, 0.0600),
    "SKU_OD":   ({"instanceType": "t3.medium", "usagetype": "BoxUsage:t3.medium", "tenancy": "Shared",
                  "operatingSystem": "Linux", "capacitystatus": "Used", "preInstalledSw": "NA",
                  "licenseModel": "No License required"}, 0.0416),
    "SKU_DED":  ({"instanceType": "t3.medium", "usagetype": "DedicatedUsage:t3.medium",
                  "tenancy": "Dedicated", "operatingSystem": "Linux", "capacitystatus": "Used",
                  "preInstalledSw": "NA", "licenseModel": "No License required"}, 0.0900),
})

# Lightsail creates a database with relationalDatabaseBundleId=micro_2_0 / blueprintId=postgres_16, and
# NEITHER string appears anywhere in the published Lightsail price list, which is keyed on memory and
# storage. Requiring every selector therefore matched nothing at all.
_LS = _offer({
    "SKU_DB":  ({"memory": "1GB", "storage": "40GB", "group": "Lightsail Database",
                 "operation": "RelationalDatabase", "highAvailability": "No",
                 "usagetype": "USE1-DatabaseUsage:1GB"}, 0.02016),
    "SKU_DBHA": ({"memory": "1GB", "storage": "40GB", "group": "Lightsail Database",
                  "operation": "RelationalDatabase", "highAvailability": "Yes",
                  "usagetype": "USE1-DatabaseUsage:1GB_ha"}, 0.04032),
})


class TestOnDemandIsTheDefinitionNotAGuess(unittest.TestCase):
    """C19 says PUBLIC ON-DEMAND LIST. Applying that is what collapses 60 SKUs to one."""

    def test_the_on_demand_line_is_selected_not_the_first_match(self):
        with mock.patch.object(px, "region_offer", return_value=_EC2):
            hits = px.find_sku("AmazonEC2", "us-east-1", {"instanceType": "t3.medium"})
            kept = px.ondemand_only(hits)
        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0]["usagetype"], "UnusedBox:t3.medium",
                         "fixture must order the WRONG sku first, or this test proves nothing")
        self.assertEqual(len(kept), 1, [h["usagetype"] for h in kept])
        self.assertEqual(kept[0]["usagetype"], "BoxUsage:t3.medium")
        self.assertAlmostEqual(kept[0]["prices"][0]["usd"], 0.0416)

    def test_case_folded_pins(self):
        """RDS writes 'No license required', EC2 writes 'No License required'. An exact compare on the
        pin silently discarded every RDS on-demand SKU."""
        rds = _offer({"S": ({"instanceType": "db.t3.micro", "usagetype": "InstanceUsage:db.t3.micro",
                             "deploymentOption": "Single-AZ",
                             "licenseModel": "No license required"}, 0.018)})
        with mock.patch.object(px, "region_offer", return_value=rds):
            kept = px.ondemand_only(px.find_sku("AmazonRDS", "us-east-1",
                                                {"instanceType": "db.t3.micro"}))
        self.assertEqual(len(kept), 1, "a lowercase 'license' must not discard the on-demand SKU")


class TestVendorVocabularyDoesNotBlockTheMatch(unittest.TestCase):
    """A value the service's price list never uses is not a selector for that service."""

    def test_unmatchable_identifiers_are_dropped_not_required(self):
        sel = {"relationalDatabaseBundleId": "micro_2_0",     # in NO SKU
               "relationalDatabaseBlueprintId": "postgres_16",  # in NO SKU
               "memory": "1GB", "storage": "40GB"}             # these ARE the SKU's keys
        with mock.patch.object(px, "region_offer", return_value=_LS):
            hits = px.find_sku("AmazonLightsail", "us-east-1", sel)
            kept = px.ondemand_only(hits)
        self.assertEqual(len(hits), 2, "the bundle id must not veto the match")
        self.assertEqual(len(kept), 1, "high availability is a choice, not the default")
        self.assertEqual(kept[0]["usagetype"], "USE1-DatabaseUsage:1GB")

    def test_a_selector_matching_nothing_anywhere_still_returns_nothing(self):
        """Dropping unmatchable values must not turn 'no match' into 'any match'."""
        with mock.patch.object(px, "region_offer", return_value=_LS):
            hits = px.find_sku("AmazonLightsail", "us-east-1", {"memory": "512GB"})
        self.assertEqual(hits, [], "an unknown size must not fall through to an arbitrary SKU")


if __name__ == "__main__":
    unittest.main()
