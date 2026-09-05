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

    def test_a_verb_outside_the_fallback_list_still_yields_its_noun(self):
        """The noun is the operation minus its leading verb WORD, not minus a listed verb. AWS's own
        registry classifies CopySnapshot and StartInstances as creates, and both were invisible while
        thirteen typed verbs decided what a create was; their nouns have to work too."""
        self.assertEqual(_kind_of("CopySnapshot"), "Snapshot")
        self.assertEqual(_kind_of("StartInstances"), "Instances")
        self.assertEqual(_kind_of("PutBucketPolicy"), "BucketPolicy")
        self.assertEqual(_kind_of("ImportKeyPair"), _kind_of("CreateKeyPair"),
                         "two ways of standing up the same kind must pair on the same noun")
        self.assertEqual(_kind_of(""), "")


class TestAwsSaysWhatCreatesAndWhatDeletes(unittest.TestCase):
    """Thirteen English verbs used to BE the classifier. An operation outside them was not merely
    unpriced, it never entered discovery, so its cost was silently absent. AWS publishes the answer in
    every CloudFormation resource schema (`handlers.create.permissions` / `handlers.delete.permissions`).
    """

    def setUp(self):
        from acspeed.adapters import aws_ct_cost as m
        self.m = m
        m._LIFECYCLE.clear()
        self.addCleanup(m._LIFECYCLE.clear)

    def _with(self, create=(), delete=()):
        from unittest import mock
        return mock.patch.object(self.m, "_lifecycle",
                                 return_value={"create": set(create), "delete": set(delete)})

    def test_a_create_verb_nobody_typed_is_classified_from_the_registry(self):
        with self._with(create={"ec2:CopySnapshot", "ec2:StartInstances"}):
            self.assertEqual(self.m.classify_event("ec2", "CopySnapshot"), "create")
            self.assertEqual(self.m.classify_event("ec2", "StartInstances"), "create")

    def test_those_same_events_are_invisible_to_the_verb_list_alone(self):
        """The premise. Without this the test above proves nothing: it would pass on a classifier that
        ignored the registry entirely."""
        with self._with():
            self.assertIsNone(self.m.classify_event("ec2", "CopySnapshot"))
            self.assertIsNone(self.m.classify_event("ec2", "StartInstances"))

    def test_the_verbs_still_answer_when_the_registry_is_silent(self):
        """Offline, or for an operation no resource type mentions, coverage must fall back to what it
        was rather than to nothing."""
        with self._with():
            self.assertEqual(self.m.classify_event("lightsail", "CreateRelationalDatabase"), "create")
            self.assertEqual(self.m.classify_event("ec2", "TerminateInstances"), "delete")

    def test_an_ambiguous_operation_is_left_to_the_verbs(self):
        """`ec2:AllocateAddress` sits in four create handlers AND one delete handler. A source that says
        both says nothing."""
        with self._with(create={"ec2:AllocateAddress"}, delete={"ec2:AllocateAddress"}):
            self.assertEqual(self.m.classify_event("ec2", "AllocateAddress"), "create")

    def test_a_read_is_neither(self):
        with self._with(create={"ec2:DescribeVolumes"}, delete={"ec2:DescribeVolumes"}):
            self.assertIsNone(self.m.classify_event("ec2", "DescribeVolumes"))


if __name__ == "__main__":
    unittest.main()
