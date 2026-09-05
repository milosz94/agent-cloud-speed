"""The noun of a create event is the resource's name, and the SKU anchor is built from it.

`_kind_of` strips the verb off a CloudTrail event name. It did so with a regex that MATCHED the capital
starting the noun, and `re.sub` removes everything it matches, so the first letter of every resource
kind in every run was silently eaten: CreateKeyPair -> "eyPair", RunInstances -> "nstances". Delete
pairing did not notice (creates and deletes were mangled identically) which is why 654 tests stayed
green, but the anchor that decides whether a SKU is about this KIND of resource was being handed
"etworkinterface", and every run's per-resource listing printed a word that is not a word.
"""
import unittest

from acspeed.adapters.aws_ct_cost import _kind_of

# Event names taken verbatim from CloudTrail on account 776638915601, run acscdb8a4d2.
_REAL = {
    "CreateRelationalDatabase": "RelationalDatabase",
    "CreateKeyPair": "KeyPair",
    "CreateNetworkInterface": "NetworkInterface",
    "CreateTargetGroup": "TargetGroup",
    "CreateLoadBalancer": "LoadBalancer",
    "CreateContainerService": "ContainerService",
    "RunInstances": "Instances",
    "AllocateAddress": "Address",
    "DeleteKeyPair": "KeyPair",
    "TerminateInstances": "Instances",
    "ReleaseAddress": "Address",
}


class TestTheNounSurvivesTheVerb(unittest.TestCase):

    def test_the_first_letter_of_the_noun_is_kept(self):
        for event, noun in _REAL.items():
            self.assertEqual(_kind_of(event), noun, f"{event} lost its noun")

    def test_a_delete_still_pairs_with_its_create(self):
        """The netting rule joins on this noun; keeping the letter must not break the join."""
        self.assertEqual(_kind_of("CreateKeyPair"), _kind_of("DeleteKeyPair"))
        self.assertEqual(_kind_of("RunInstances"), _kind_of("TerminateInstances"))
        self.assertEqual(_kind_of("AllocateAddress"), _kind_of("ReleaseAddress"))

    def test_a_verb_that_is_only_a_prefix_of_a_longer_word_is_not_stripped(self):
        """`Creates`/`Running` are not the verb; the lookahead requires a capital to follow."""
        self.assertEqual(_kind_of("Createsomething"), "Createsomething")
        self.assertEqual(_kind_of("Register"), "Register")

    def test_an_event_with_no_known_verb_is_returned_whole(self):
        self.assertEqual(_kind_of("PutBucketPolicy"), "PutBucketPolicy")
        self.assertEqual(_kind_of(""), "")


if __name__ == "__main__":
    unittest.main()
