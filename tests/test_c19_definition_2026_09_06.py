"""The only tables left in the pricing path must be the PAPER's definition, not the vendor's.

Everything else that was typed out here has been derived from what AWS publishes: the eventSource ->
service-code map, the value-spelling map, the region-name map, the region usagetype prefixes, the
billable-create list, the free-create list, the delete-pairing list, the resource-id prefix list, the
identity field names, the sized-child field names, and the thirteen create/delete verbs. What remains
states what C19 MEASURES (a standing hourly run-rate at the public on-demand list price), which is a
property of the paper and cannot be looked up anywhere.

This test exists so that distinction stays true. A new table added to the pricing modules will either
carry a stated C19 justification or fail here.
"""
import re
import unittest
from pathlib import Path

from acspeed.adapters import aws_price_index as px

_MODULES = [Path(px.__file__),
            Path(px.__file__).with_name("aws_ct_cost.py")]


class TestC19DefinitionIsDeclared(unittest.TestCase):

    def test_every_pin_says_which_clause_it_implements(self):
        for attribute, entry in px._ONDEMAND_PINS.items():
            self.assertIsInstance(entry, tuple, f"{attribute} must be (value, why)")
            value, why = entry
            self.assertTrue(str(value).strip(), f"{attribute} has no value")
            self.assertGreater(len(str(why).split()), 4,
                               f"{attribute} pins the price list without saying why; a pin with no "
                               f"stated reason is vendor knowledge wearing a definition's clothes")

    def test_the_pins_still_apply(self):
        """The justification is documentation; this is the behaviour. An Outposts SKU sits in the same
        offer file as the region's and is a different product at a different rate."""
        offer = {"products": {
            "REGION": {"attributes": {"usagetype": "LoadBalancerUsage", "locationType": "AWS Region"}},
            "OUTPOST": {"attributes": {"usagetype": "Outposts-LoadBalancerUsage",
                                       "locationType": "AWS Outposts"}}},
            "terms": {"OnDemand": {
                sku: {f"{sku}.T": {"priceDimensions": {f"{sku}.T.D": {
                    "unit": "Hrs", "pricePerUnit": {"USD": "0.0225"}}}}}
                for sku in ("REGION", "OUTPOST")}}}
        matches = [{"sku": s, "usagetype": p["attributes"]["usagetype"], "attributes": p["attributes"],
                    "prices": [{"unit": "Hrs", "usd": 0.0225, "description": ""}]}
                   for s, p in offer["products"].items()]
        kept = [m["usagetype"] for m in px.ondemand_only(matches)]
        self.assertEqual(kept, ["LoadBalancerUsage"])


class TestNoUndeclaredTableCreptBack(unittest.TestCase):
    """A guard against the shape of every defect in this module's history: a list of vendor strings,
    typed by hand, wrong by omission for whatever it had not heard of."""

    # Collections that are NOT vendor vocabulary: English word suffixes and prefixes, the price list's
    # own schema field names, and the C19 tables themselves (covered by the test above).
    _ALLOWED = {
        '("name", "identifier")', '("name", "identifier", "id")',
        '("Get", "Lis", "Des")',
        '("usagetype", "group", "productFamily", "operation")',
        '("unused", "reservation", "reserved", "dedicated", "hostbox", "hostusage",',
    }

    def test_string_tuples_in_the_pricing_path_are_accounted_for(self):
        offenders = []
        for path in _MODULES:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or '"""' in stripped:
                    continue
                for found in re.findall(r'\((?:\s*"[^"]{2,}"\s*,){2,}[^)]*\)?', line):
                    if any(a in line for a in self._ALLOWED):
                        continue
                    offenders.append(f"{path.name}:{number}: {found.strip()}")
        self.assertEqual(offenders, [],
                         "a new hand-written string table appeared in the pricing path; derive it from "
                         "what AWS publishes, or add it to _ALLOWED with the reason it is not vendor "
                         "vocabulary")


if __name__ == "__main__":
    unittest.main()
