"""AWS capability SSH-endpoint resolution (acspeed/adapters/aws_capability.py): C is measurable only on a
raw shell-reachable EC2 VM; every other AWS app front (Lightsail container, App Runner, ALB, CloudFront)
resolves to None -> capability disclosed N/A."""
import os
import tempfile
import unittest

from acspeed.adapters.aws_capability import (resolve_aws_ssh_endpoint, aws_key_candidates, AWS_SSH_USERS)


class TestEndpoint(unittest.TestCase):
    def test_raw_ec2_public_dns_is_ssh_reachable(self):
        for url in ("https://ec2-1-2-3-4.compute-1.amazonaws.com",
                    "http://ec2-5-6-7-8.us-west-2.compute.amazonaws.com:8080",
                    "ec2-9-9-9-9.eu-west-1.compute.amazonaws.com"):
            ep = resolve_aws_ssh_endpoint(url)
            self.assertIsNotNone(ep, url)
            self.assertEqual(ep[1], 22)
            self.assertIn(".amazonaws.com", ep[0])

    def test_shell_less_fronts_return_none_na(self):
        for url in ("https://it-tools.x.us-east-1.cs.amazonlightsail.com",   # Lightsail container
                    "https://app.awsapprunner.com",                          # App Runner
                    "https://my-alb-123.us-east-1.elb.amazonaws.com",        # behind an ALB
                    "https://d123.cloudfront.net",                           # CloudFront
                    "https://example.com"):
            self.assertIsNone(resolve_aws_ssh_endpoint(url), url)

    def test_users_amazonlinux_first(self):
        self.assertEqual(AWS_SSH_USERS[0], "ec2-user")      # AL2 default; ubuntu etc. follow
        self.assertIn("ubuntu", AWS_SSH_USERS)


class TestKeyCandidates(unittest.TestCase):
    def test_offers_private_keys_skips_pub_and_meta(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("id_ed25519", "id_ed25519.pub", "aws-abc.pem", "known_hosts", "config",
                      "authorized_keys"):
                open(os.path.join(d, n), "w").close()
            cands = [os.path.basename(p) for p in aws_key_candidates(d)]
            self.assertIn("id_ed25519", cands)
            self.assertIn("aws-abc.pem", cands)             # unknown AWS keypair name still offered
            for skip in ("id_ed25519.pub", "known_hosts", "config", "authorized_keys"):
                self.assertNotIn(skip, cands)

    def test_missing_dir_is_empty(self):
        self.assertEqual(aws_key_candidates("/no/such/dir/xyz"), [])


if __name__ == "__main__":
    unittest.main()
