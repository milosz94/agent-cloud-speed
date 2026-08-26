"""The cloud-agnostic capability runner, exercised end-to-end through a FAKE shell.

No tool, no VM, no network: a fake ``exec_fn`` returns captured probe outputs, so we test command
construction (the grounded probes + C11 queue depths) and the full probe -> scalar -> CVector -> DCI
chain offline. The same runner, given a real local_exec / ssh_exec, measures the reference host or any
cloud's VM.
"""
import json
import unittest

from acspeed import capability, capability_probe as cp, probes
from acspeed.types import CVector

SYSBENCH_SINGLE_OUT = "CPU speed:\n    events per second:   950.25\n"
SYSBENCH_ALLCORE_OUT = "CPU speed:\n    events per second:  7300.80\n"
STREAM_OUT = (
    "Function    Best Rate MB/s  Avg time     Min time     Max time\n"
    "Copy:           18000.0     0.001        0.001        0.002\n"
    "Scale:          17000.0     0.001        0.001        0.002\n"
    "Add:            19000.0     0.001        0.001        0.002\n"
    "Triad:          19500.0     0.001        0.001        0.002\n"
)
FIO_IOPS_OUT = json.dumps({"jobs": [{"read": {"iops": 52000.0, "bw": 208000.0,
                                              "clat_ns": {"mean": 610000.0}},
                                     "write": {"iops": 0.0, "bw": 0.0}}]})
FIO_BW_OUT = json.dumps({"jobs": [{"read": {"iops": 900.0, "bw": 950000.0,
                                            "clat_ns": {"mean": 1100000.0}},
                                   "write": {"iops": 0.0, "bw": 0.0}}]})
FIO_LAT_OUT = json.dumps({"jobs": [{"read": {"iops": 8000.0, "bw": 32000.0,
                                             "clat_ns": {"mean": 118000.0}},
                                    "write": {"iops": 0.0, "bw": 0.0}}]})
IPERF_TCP_OUT = json.dumps({
    "intervals": [{"sum": {"bits_per_second": 910000000.0}},
                  {"sum": {"bits_per_second": 930000000.0}}],
    "end": {"sum_received": {"bits_per_second": 920000000.0},
            "sum_sent": {"bits_per_second": 950000000.0}}})
PING_OUT = (
    "PING 10.0.0.5 (10.0.0.5) 56(84) bytes of data.\n"
    "64 bytes from 10.0.0.5: icmp_seq=1 ttl=64 time=0.312 ms\n"
    "64 bytes from 10.0.0.5: icmp_seq=2 ttl=64 time=0.401 ms\n"
    "\n--- 10.0.0.5 ping statistics ---\n"
    "20 packets transmitted, 20 received, 0% packet loss, time 19000ms\n"
    "rtt min/avg/max/mdev = 0.298/0.377/0.512/0.061 ms\n"
)


class FakeShell:
    """Dispatches a command string to a captured output; records what it was asked to run."""

    def __init__(self):
        self.seen = []

    def __call__(self, command, timeout_s):
        self.seen.append(command)
        c = command
        if "apt-get" in c:
            return 0, "INSTALL_OK\n", ""
        if "nproc" in c and "sysbench" not in c:
            return 0, "nproc=8\nmem_kb=16000000\nkernel=7.0.0\ncpu=Test CPU\n", ""
        if "gcc" in c and "stream.c" in c:
            return 0, "BUILD_OK\n", ""
        if c.strip() == "/tmp/stream_bin":
            return 0, STREAM_OUT, ""
        if "sysbench" in c:
            return (0, SYSBENCH_SINGLE_OUT, "") if "--threads=1" in c else (0, SYSBENCH_ALLCORE_OUT, "")
        if "fio" in c:
            if "iodepth=1 " in c:
                return 0, FIO_LAT_OUT, ""
            if "bs=1M" in c:
                return 0, FIO_BW_OUT, ""
            return 0, FIO_IOPS_OUT, ""
        if "iperf3 -s" in c or "pkill" in c:      # one-shot server start / cleanup
            return 0, "", ""
        if "iperf3 -J -c" in c:                    # VM-to-VM client throughput
            return 0, IPERF_TCP_OUT, ""
        if c.startswith("ping ") or " ping " in c:
            return 0, PING_OUT, ""
        if c.startswith("rm -f"):
            return 0, "", ""
        return 0, "", ""


class TestCommandConstruction(unittest.TestCase):
    def test_fio_queue_depths_match_c11(self):
        # C11: 4KiB random QD32 -> IOPS; 1MiB seq QD32 -> BW; QD1 -> latency; all O_DIRECT.
        self.assertIn("--bs=4k", cp.FIO_IOPS)
        self.assertIn("--iodepth=32", cp.FIO_IOPS)
        self.assertIn("--bs=1M", cp.FIO_BW)
        self.assertIn("--iodepth=32", cp.FIO_BW)
        self.assertIn("--iodepth=1", cp.FIO_LAT)
        for c in (cp.FIO_IOPS, cp.FIO_BW, cp.FIO_LAT):
            self.assertIn("--direct=1", c)  # O_DIRECT

    def test_sysbench_split(self):
        self.assertIn("--threads=1", cp.SYSBENCH_SINGLE)         # SPECspeed
        self.assertIn("--threads=$(nproc)", cp.SYSBENCH_ALLCORE)  # SPECrate

    def test_network_commands_are_vm_to_vm_private(self):
        # C17/C18: the network axis is measured VM-to-VM over the peer's PRIVATE IP, never to a
        # neutral/external vantage; the iperf3 server is one-shot; RTT via ping. No `_iperf_cmd`
        # (the C10 neutral-vantage builder) exists any more.
        self.assertFalse(hasattr(cp, "_iperf_cmd"), "the C10 neutral-vantage iperf builder must be gone")
        client = cp._iperf_client_cmd("10.0.0.5")
        self.assertIn("-c 10.0.0.5", client)          # the peer's private IP
        self.assertNotIn("-u", client)                # TCP only (no UDP neutral-vantage leftover)
        self.assertIn("-1", cp._iperf_server_cmd())   # one-shot server (exits after one client)
        self.assertIn("10.0.0.5", cp._ping_cmd("10.0.0.5"))


class TestEndToEnd(unittest.TestCase):
    def test_full_vector_and_dci(self):
        # multi-VM operation: a VM-to-VM network result is supplied, so network IS a scored axis (C17).
        shell = FakeShell()
        network = {"tcp_bps": 920000000.0, "rtt_ms_min": 0.298, "rtt_ms_avg": 0.377,
                   "raw": {"iperf_tcp": {"received_bps": 920000000.0}}, "errors": {}}
        res = cp.run_capability(shell, network=network,
                                stream_c_source="/* stream */", do_install=True, sudo="sudo ")
        # every axis present -> a CVector exists
        self.assertIsNotNone(res.cvector)
        self.assertEqual(res.scalars["compute"], 7300.80)       # all-core sysbench
        self.assertAlmostEqual(res.scalars["memory"], 19.5)      # 19500 MB/s Triad -> GB/s
        self.assertEqual(res.scalars["disk"], 52000.0)           # 4k QD32 read IOPS
        self.assertEqual(res.scalars["network"], 920000000.0)    # VM-to-VM iperf TCP received bps
        # network is disclosed as the scored VM-to-VM regime, with RTT as a disclosed sub-metric
        self.assertTrue(res.disclosure["network"]["applicable"])
        self.assertEqual(res.disclosure["network"]["regime"], "vm_to_vm_private")
        self.assertAlmostEqual(res.disclosure["network"]["rtt_ms_min"], 0.298)
        # disclosed extras present
        self.assertEqual(res.raw["sysbench_single_eps"], 950.25)
        self.assertAlmostEqual(res.raw["fio_lat_us_qd1"], 118.0)  # 118000 ns -> us
        self.assertIn("net_vm_to_vm", res.raw)
        self.assertEqual(res.errors, {})
        # DCI against a reference is finite and 1.0 vs itself
        dci_self = capability.dci(res.cvector, res.cvector)
        self.assertAlmostEqual(dci_self, 1.0)

    def test_single_vm_network_is_not_applicable_not_error(self):
        # single-VM operation (no NetworkPair) + no stream source -> network N/A (disclosed, NOT an
        # error, C18) and memory absent; the run still succeeds on the axes that ran.
        shell = FakeShell()
        res = cp.run_capability(shell, network=None, nominal_nic="virtio 1 Gbps",
                                stream_c_source=None, do_install=False)
        self.assertIsNone(res.cvector)                            # incomplete -> no headline CVector
        self.assertIsNone(res.scalars["network"])
        self.assertIsNone(res.scalars["memory"])
        self.assertEqual(res.scalars["compute"], 7300.80)         # the axes that ran are still there
        self.assertEqual(res.scalars["disk"], 52000.0)
        # network is NOT an error: it is a disclosed not-applicable axis with the nominal NIC recorded
        self.assertNotIn("network", res.errors)
        self.assertFalse(res.disclosure["network"]["applicable"])
        self.assertEqual(res.disclosure["network"]["nominal_nic"], "virtio 1 Gbps")
        self.assertIn("memory", res.errors)                       # memory absence IS an error (probe skipped)
        self.assertEqual(sorted(res.to_dict()["axes_present"]), ["compute", "disk"])

    def test_residency_disclosure_present(self):
        res = cp.run_capability(FakeShell(), network=None,
                                stream_c_source="/* stream */", do_install=False)
        self.assertEqual(res.disclosure["residency"], "app-resident")
        self.assertIn("under residency", res.disclosure["note"].lower())
        self.assertIn("disclosure", res.to_dict())

    def test_measure_network_pair_vm_to_vm(self):
        # the two-VM orchestration (C17): monkeypatch ssh_exec so both handles run the FakeShell, then
        # confirm iperf3 TCP + ping RTT are measured over the peer's PRIVATE IP and reduced correctly,
        # WITH the C17 E1 disclosure controls (placement/co-residency, stream count, window, distribution).
        import acspeed.capability_probe as capmod
        orig = capmod.ssh_exec
        capmod.ssh_exec = lambda handle, **kw: FakeShell()
        try:
            client = cp.SSHHandle(host="pub.c", user="ubuntu", private_key_path="/k/id", port=22001,
                                  instance_id="i-client")
            server = cp.SSHHandle(host="pub.s", user="ubuntu", private_key_path="/k/id", port=22002,
                                  instance_id="i-server")
            pair = cp.NetworkPair(client=client, server=server, server_private_ip="10.0.0.5",
                                  co_residency="anti-affinity")
            out = cp.measure_network_pair(pair, do_install=False)
        finally:
            capmod.ssh_exec = orig
        self.assertEqual(out["tcp_bps"], 920000000.0)
        self.assertEqual(out["tcp_bps_distribution"], [910000000.0, 930000000.0])  # C17 E1 distribution
        self.assertAlmostEqual(out["rtt_ms_min"], 0.298)
        self.assertAlmostEqual(out["rtt_ms_max"], 0.512)
        self.assertAlmostEqual(out["rtt_ms_mdev"], 0.061)
        self.assertEqual(out["stream_count"], 1)                    # C17 E1 disclosed
        self.assertIsNone(out["tcp_window"])                        # C17 E1 disclosed (OS default)
        self.assertEqual(out["placement"]["co_residency"], "anti-affinity")   # C17 E1 co-residency
        self.assertEqual(out["placement"]["client_instance_id"], "i-client")
        self.assertEqual(out["placement"]["server_instance_id"], "i-server")
        self.assertEqual(out["errors"], {})

    def test_network_client_cmd_streams_and_window_are_explicit(self):
        # C17 E1: a single flow under-reports a fast NIC, so -P (streams) and -w (window) are explicit.
        base = cp._iperf_client_cmd("10.0.0.5")
        self.assertNotIn("-P", base)                               # default: single flow, no -P noise
        multi = cp._iperf_client_cmd("10.0.0.5", streams=8, window="256K")
        self.assertIn("-P 8", multi)
        self.assertIn("-w 256K", multi)

    def test_normalize_network_rtt_specspeed_inversion(self):
        # r_rtt = RTT_ref / RTT_measured (reference on top): a faster (lower) RTT scores > 1.
        self.assertAlmostEqual(cp.normalize_network_rtt(0.2, 0.4), 2.0)
        self.assertIsNone(cp.normalize_network_rtt(0.2, None))     # no frozen reference VM-pair yet
        self.assertIsNone(cp.normalize_network_rtt(None, 0.4))


class TestSSHHandleArgv(unittest.TestCase):
    def test_direct_handle_no_proxyjump(self):
        h = cp.SSHHandle(host="1.2.3.4", user="ubuntu", private_key_path="/k/id", port=22)
        argv = cp._ssh_argv(h)
        self.assertIn("-i", argv)
        self.assertIn("/k/id", argv)
        self.assertIn("-p", argv)
        self.assertIn("22", argv)
        self.assertEqual(argv[-1], "ubuntu@1.2.3.4")
        self.assertFalse(any(str(a).startswith("ProxyJump=") for a in argv))  # generic: no bastion

    def test_bastion_handle_adds_proxyjump_and_port(self):
        h = cp.SSHHandle(host="10.50.0.5", user="ubuntu", private_key_path="/k/id",
                         port=22001, proxy_jump="jump@edge.redu.cloud:22")
        argv = cp._ssh_argv(h)
        self.assertIn("22001", argv)                                   # the redu stream port, generically
        self.assertIn("ProxyJump=jump@edge.redu.cloud:22", argv)


class TestKeypair(unittest.TestCase):
    def test_generate_run_keypair(self):
        import os, shutil, tempfile
        if shutil.which("ssh-keygen") is None:
            self.skipTest("ssh-keygen not installed")
        d = tempfile.mkdtemp(prefix="acspeed-kt-")
        try:
            priv, pub = cp.generate_run_keypair(d)
            self.assertTrue(os.path.exists(priv))
            self.assertTrue(pub.startswith("ssh-ed25519"))
            self.assertEqual(oct(os.stat(priv).st_mode)[-3:], "600")   # harness holds the private key
        finally:
            shutil.rmtree(d, ignore_errors=True)


def _fake_run(returncodes):
    """subprocess.run stub yielding the given return codes in order."""
    import types
    seq = iter(returncodes)
    calls = {"n": 0}

    def run(argv, capture_output, text, timeout):
        calls["n"] += 1
        rc = next(seq)
        return types.SimpleNamespace(returncode=rc, stdout=("ok" if rc == 0 else ""), stderr="")
    return run, calls


class TestSSHExecRetry(unittest.TestCase):
    def test_retries_only_on_255(self):
        h = cp.SSHHandle(host="h", user="u", private_key_path="/k")
        run, calls = _fake_run([255, 255, 0])
        orig = cp.subprocess.run
        cp.subprocess.run = run
        try:
            rc, out, _ = cp.ssh_exec(h, ssh_retries=5)("echo hi", 30)
            self.assertEqual(rc, 0)
            self.assertEqual(out, "ok")
            self.assertEqual(calls["n"], 3)              # two transport failures, then success
        finally:
            cp.subprocess.run = orig

    def test_real_nonzero_rc_returns_immediately(self):
        h = cp.SSHHandle(host="h", user="u", private_key_path="/k")
        run, calls = _fake_run([1, 0, 0])                # a real command failure, never retried
        orig = cp.subprocess.run
        cp.subprocess.run = run
        try:
            rc, _, _ = cp.ssh_exec(h, ssh_retries=5)("false", 30)
            self.assertEqual(rc, 1)
            self.assertEqual(calls["n"], 1)
        finally:
            cp.subprocess.run = orig


class TestWaitFirstSSH(unittest.TestCase):
    def test_returns_on_first_success(self):
        h = cp.SSHHandle(host="h", user="u", private_key_path="/k")
        seq = iter([1, 1, 0])
        st = {"now": 0.0}
        orig = cp.ssh_exec
        cp.ssh_exec = lambda handle, ssh_retries=1: (lambda cmd, t: (next(seq), "", ""))
        try:
            r = cp.wait_first_ssh(h, timeout_s=100, poll_interval=3.0,
                                  clock=lambda: st["now"],
                                  sleep=lambda s: st.__setitem__("now", st["now"] + s))
            self.assertIsNotNone(r)
        finally:
            cp.ssh_exec = orig

    def test_timeout_returns_none(self):
        h = cp.SSHHandle(host="h", user="u", private_key_path="/k")
        st = {"now": 0.0}
        orig = cp.ssh_exec
        cp.ssh_exec = lambda handle, ssh_retries=1: (lambda cmd, t: (1, "", ""))
        try:
            r = cp.wait_first_ssh(h, timeout_s=10, poll_interval=6.0,
                                  clock=lambda: st["now"],
                                  sleep=lambda s: st.__setitem__("now", st["now"] + s))
            self.assertIsNone(r)
        finally:
            cp.ssh_exec = orig


class TestJSONParserRobustness(unittest.TestCase):
    # regression: on a deploy VM the login shell prepended a banner before fio/iperf3 JSON, so strict
    # json.loads failed at char 0 (umami run, 2026-08-26). The JSON parsers now extract the object.
    def test_parse_fio_tolerates_leading_banner(self):
        junk = "Welcome to Ubuntu 24.04\nLast login: Mon\n" + FIO_IOPS_OUT
        self.assertEqual(probes.parse_fio(junk)["read_iops"], 52000.0)

    def test_parse_iperf3_tolerates_leading_banner(self):
        self.assertEqual(probes.parse_iperf3("profile.d echo\n" + IPERF_TCP_OUT)["received_bps"],
                         920000000.0)

    def test_fio_latency_tolerates_leading_banner(self):
        self.assertAlmostEqual(cp.parse_fio_latency_us("motd\n" + FIO_LAT_OUT), 118.0)

    def test_no_json_gives_clear_error(self):
        with self.assertRaises(ValueError):
            probes.parse_fio("fio: engine libaio not loadable\nfio: failed to load engine")


class TestParseSSHCommand(unittest.TestCase):
    def test_basic_with_injected_key(self):
        h = cp.parse_ssh_command("ssh -p 22004 ubuntu@host.redu.cloud", private_key_path="/k/id")
        self.assertEqual((h.host, h.port, h.user, h.private_key_path),
                         ("host.redu.cloud", 22004, "ubuntu", "/k/id"))

    def test_inline_i_wins_and_opts_skipped(self):
        h = cp.parse_ssh_command(
            "ssh -i /home/u/.ssh/redu -o IdentitiesOnly=yes -p 22011 ubuntu@vm-abc.redu.cloud")
        self.assertEqual(h.port, 22011)
        self.assertEqual(h.private_key_path, "/home/u/.ssh/redu")
        self.assertEqual(h.host, "vm-abc.redu.cloud")

    def test_no_key_raises(self):
        with self.assertRaises(ValueError):
            cp.parse_ssh_command("ssh -p 22 ubuntu@h")

    def test_no_target_raises(self):
        with self.assertRaises(ValueError):
            cp.parse_ssh_command("ssh -p 22", private_key_path="/k")


class TestNormalize(unittest.TestCase):
    def test_partial_ratios_and_geomean(self):
        n = cp.normalize_against_reference(
            {"compute": 100.0, "memory": None, "disk": 50.0, "network": None},
            {"compute": 200.0, "memory": 10.0, "disk": 100.0, "network": None})
        self.assertEqual(n["axes"], ["compute", "disk"])           # memory skipped (vm None)
        self.assertAlmostEqual(n["ratios"]["compute"], 0.5)
        self.assertAlmostEqual(n["dci_partial"], 0.5)              # geomean(0.5, 0.5)

    def test_empty_dci_none(self):
        n = cp.normalize_against_reference({"compute": None}, {"compute": 1.0})
        self.assertIsNone(n["dci_partial"])
        self.assertEqual(n["axes"], [])


class TestRunCapabilityOnHandle(unittest.TestCase):
    def test_orchestration_with_fakes(self):
        h = cp.SSHHandle(host="h", user="u", private_key_path="/k", port=22004)
        orig_wait, orig_exec = cp.wait_first_ssh, cp.ssh_exec
        cp.wait_first_ssh = lambda handle, timeout_s=180, **k: 123.0
        cp.ssh_exec = lambda handle, ssh_retries=3: FakeShell()
        try:
            rep = cp.run_capability_on_handle(
                h, reference_scalars={"compute": 3650.4, "memory": 9.75, "disk": 26000.0},
                stream_c_source="/* stream */", do_install=False, residency="app-resident")
            self.assertTrue(rep["ok"])
            self.assertEqual(rep["endpoint"], "u@h:22004")
            self.assertIn("normalized", rep)
            self.assertEqual(rep["normalized"]["axes"], ["compute", "disk", "memory"])
            self.assertGreater(rep["normalized"]["dci_partial"], 0)
        finally:
            cp.wait_first_ssh, cp.ssh_exec = orig_wait, orig_exec

    def test_ssh_never_ready(self):
        h = cp.SSHHandle(host="h", user="u", private_key_path="/k")
        orig = cp.wait_first_ssh
        cp.wait_first_ssh = lambda handle, timeout_s=180, **k: None
        try:
            rep = cp.run_capability_on_handle(h, ssh_ready_timeout=5)
            self.assertFalse(rep["ok"])
            self.assertIn("no SSH", rep["error"])
        finally:
            cp.wait_first_ssh = orig


class TestTryKeys(unittest.TestCase):
    def _mkkeys(self, *names):
        import os, tempfile
        d = tempfile.mkdtemp(prefix="acspeed-keys-")
        paths = []
        for n in names:
            p = os.path.join(d, n)
            with open(p, "w") as fh:
                fh.write("k")
            paths.append(p)
        return paths

    def test_picks_the_key_that_authenticates(self):
        bad, good = self._mkkeys("acspeed-cap", "redu-app-deploy")
        used = {}
        orig_e, orig_r = cp.ssh_exec, cp.run_capability_on_handle
        cp.ssh_exec = lambda handle, ssh_retries=1: (
            lambda cmd, t: (0, "", "") if handle.private_key_path == good else (255, "", ""))

        def _fake_roch(h, **kw):
            used["key"] = h.private_key_path
            return {"ok": True}
        cp.run_capability_on_handle = _fake_roch
        try:
            rep = cp.run_capability_try_keys("h", "u", 22, [bad, good], total_timeout=30,
                                             sleep=lambda s: None)
            self.assertTrue(rep["ok"])
            self.assertEqual(used["key"], good)               # tried bad (255), used good (0)
        finally:
            cp.ssh_exec, cp.run_capability_on_handle = orig_e, orig_r

    def test_no_working_key_times_out_cleanly(self):
        (k,) = self._mkkeys("redu-x")
        st = {"t": 0.0}
        orig = cp.ssh_exec
        cp.ssh_exec = lambda handle, ssh_retries=1: (lambda cmd, t: (255, "", ""))
        try:
            rep = cp.run_capability_try_keys("h", "u", 22, [k], total_timeout=10,
                                             clock=lambda: st["t"],
                                             sleep=lambda s: st.__setitem__("t", st["t"] + s))
            self.assertFalse(rep["ok"])
            self.assertIn("no SSH", rep["error"])
        finally:
            cp.ssh_exec = orig

    def test_no_candidate_keys(self):
        rep = cp.run_capability_try_keys("h", "u", 22, ["/nope/nope"], total_timeout=5)
        self.assertFalse(rep["ok"])
        self.assertIn("no candidate", rep["error"])


class TestCapabilityAdapter(unittest.TestCase):
    def test_is_abstract(self):
        with self.assertRaises(TypeError):
            cp.CapabilityAdapter()

    def test_subclass_resolves_handle(self):
        class MockAdapter(cp.CapabilityAdapter):
            def ssh_handle(self, deployment_ref):
                return cp.SSHHandle(host="h", user="u", private_key_path="/k")
        h = MockAdapter().ssh_handle({"url": "x"})
        self.assertEqual(h.host, "h")


if __name__ == "__main__":
    unittest.main()
