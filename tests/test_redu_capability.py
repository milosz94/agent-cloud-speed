"""ReduCapabilityAdapter: resolves a deployment's SSH endpoint into an SSHHandle. The HTTP MCP call is
injected here so this is a pure, offline test of the adapter's boundary (parse + key policy)."""
import unittest

from acspeed.adapters.redu_capability import ReduCapabilityAdapter


class TestReduCapabilityAdapter(unittest.TestCase):
    def test_uses_deployments_own_resolved_keypair(self):
        # keypair_name is NOT forced: get_ssh_command resolves the deploy's OWN per-app key, and the
        # inline -i (its private key, recovered from the microVM into ~/.ssh) is what we authenticate with.
        seen = {}
        def fake(ref, keypair_name=None):
            seen["kp"] = keypair_name
            return f"ssh -i ~/.ssh/redu-{ref}-deploy -o IdentitiesOnly=yes -p 22004 ubuntu@{ref}.redu.cloud"
        h = ReduCapabilityAdapter(get_ssh_command=fake, private_key_path="/default/key").ssh_handle("evershop")
        self.assertIsNone(seen["kp"])                                  # adapter does not force a keypair
        self.assertEqual((h.host, h.port, h.user), ("evershop.redu.cloud", 22004, "ubuntu"))
        self.assertTrue(h.private_key_path.endswith("redu-evershop-deploy"))  # the deploy's own key

    def test_falls_back_to_default_key_when_no_inline_i(self):
        fake = lambda ref, keypair_name=None: f"ssh -p 22 ubuntu@{ref}"
        h = ReduCapabilityAdapter(get_ssh_command=fake, private_key_path="/default/acspeed-cap").ssh_handle("h1")
        self.assertEqual(h.private_key_path, "/default/acspeed-cap")

    def test_none_when_unresolved(self):
        a = ReduCapabilityAdapter(get_ssh_command=lambda ref, keypair_name=None: None)
        self.assertIsNone(a.ssh_handle("x"))


if __name__ == "__main__":
    unittest.main()
