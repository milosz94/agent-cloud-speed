"""AWS cost enumeration must match THIS deployment's resources. Regression for 2026-08-28: an ALB URL's
first DNS label carries the ELB hash (umami-acs...-1531808006), which is NOT in the resource ARNs
(umami-acs...), so the old app-name match found nothing and cost failed with "could not price the
bundle". Fix: strip the ELB hash, and anchor on the run token (in the URL and the resource names)."""
import unittest

from acspeed.adapters.aws_runrate import _app_name, _match_anchors


class TestAwsEnumAnchors(unittest.TestCase):
    ALB = "http://umami-acs3f3d357c-1531808006.us-east-1.elb.amazonaws.com"

    def test_elb_hash_stripped_from_app_name(self):
        self.assertEqual(_app_name(self.ALB), "umami-acs3f3d357c")  # hash gone

    def test_anchors_include_token_and_app_name(self):
        self.assertEqual(_match_anchors(self.ALB), ["acs3f3d357c", "umami-acs3f3d357c"])

    def test_anchors_match_the_real_resource_arns(self):
        anchors = _match_anchors(self.ALB)
        arns = [
            "arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/app/umami-acs3f3d357c/9a8b",
            "arn:aws:ecs:us-east-1:1:service/umami-acs3f3d357c-cluster/umami-acs3f3d357c",
            "arn:aws:rds:us-east-1:1:db:umami-db-acs3f3d357c",   # token-only (different base name)
        ]
        for arn in arns:
            self.assertTrue(any(a in arn for a in anchors), f"no anchor matched {arn}")

    def test_the_old_hashed_name_would_have_missed(self):
        # the pre-fix behavior (first label WITH hash) matches none of the ARNs -> the empty-bundle bug
        hashed = "umami-acs3f3d357c-1531808006"
        arn = "arn:aws:elasticloadbalancing:us-east-1:1:loadbalancer/app/umami-acs3f3d357c/9a8b"
        self.assertNotIn(hashed, arn)

    def test_non_elb_url_unchanged(self):
        # a plain host is not touched by the ELB-hash strip
        self.assertEqual(_app_name("https://myapp.example.com"), "myapp")


if __name__ == "__main__":
    unittest.main()
