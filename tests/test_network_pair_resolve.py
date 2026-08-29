"""The redu C17 network-pair resolver: the managed datastore VM shares the deploy keypair and lives on
the private network (no floating IP), so it is reached by ProxyJump THROUGH the app VM. A pair forms
whenever the deploy has a datastore with a private_ip; None only for a genuinely single-VM deploy."""
import unittest
from acspeed.adapters.redu_capability import resolve_network_pair, _first_private_ip
from acspeed.capability_probe import SSHHandle

APP = SSHHandle(host="app.redu.cloud", user="ubuntu", port=22004, private_key_path="/k")


class TestResolveNetworkPair(unittest.TestCase):
    def test_pair_jumps_through_app_vm_to_db_private_ip(self):
        def ct(tool, args):
            if tool == "get_deployment": return {"deployment": {"db_id": 42}}
            if tool == "list_databases": return {"databases": [{"id": 42, "private_ip": "10.50.0.9"}]}
            return {}
        pair = resolve_network_pair("503", APP, call_tool=ct)
        self.assertIsNotNone(pair)
        self.assertEqual(pair.server.host, "10.50.0.9")               # server = the DB private IP
        self.assertEqual(pair.server_private_ip, "10.50.0.9")
        self.assertEqual(pair.server.proxy_jump, "ubuntu@app.redu.cloud:22004")  # jump through the app VM
        self.assertEqual(pair.server.private_key_path, "/k")          # SAME keypair as the app VM
        self.assertEqual(pair.server.port, 22)                        # private-net SSH, not a stream port

    def test_redis_datastore_also_pairs(self):
        def ct(tool, args):
            if tool == "get_deployment": return {"deployment": {"redis_id": 7}}
            if tool == "list_redis": return {"redis": [{"id": 7, "member_ips": ["10.50.0.5"]}]}
            return {}
        pair = resolve_network_pair("88", APP, call_tool=ct)
        self.assertIsNotNone(pair)
        self.assertEqual(pair.server_private_ip, "10.50.0.5")

    def test_none_when_no_datastore(self):
        pair = resolve_network_pair("503", APP, call_tool=lambda t, a: {"deployment": {}})
        self.assertIsNone(pair)

    def test_first_private_ip_fields(self):
        self.assertEqual(_first_private_ip({"private_ip": "1.2.3.4"}), "1.2.3.4")
        self.assertEqual(_first_private_ip({"member_ips": ["10.0.0.1", "10.0.0.2"]}), "10.0.0.1")
        self.assertIsNone(_first_private_ip({"nope": 1}))


if __name__ == "__main__":
    unittest.main()
