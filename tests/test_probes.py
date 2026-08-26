import unittest

from acspeed import probes

SYSBENCH = (
    "CPU speed:\n"
    "    events per second:  1234.56\n"
    "General statistics:\n"
    "    total time:  10.0001s\n"
)

STREAM = (
    "Function    Best Rate MB/s  Avg time     Min time     Max time\n"
    "Copy:           12000.0     0.001333     0.001300     0.001400\n"
    "Scale:          11000.0     0.001500     0.001450     0.001600\n"
    "Add:            13000.0     0.001900     0.001850     0.002000\n"
    "Triad:          13500.0     0.001800     0.001780     0.001900\n"
)

FIO = '{"jobs":[{"read":{"iops":5000.0,"bw":20000},"write":{"iops":100.0,"bw":400}}]}'

IPERF = (
    '{"end":{"sum_received":{"bits_per_second":9400000000.0},'
    '"sum_sent":{"bits_per_second":9450000000.0}}}'
)


class TestProbes(unittest.TestCase):
    def test_sysbench(self):
        self.assertAlmostEqual(probes.parse_sysbench_cpu(SYSBENCH), 1234.56)

    def test_sysbench_missing(self):
        with self.assertRaises(ValueError):
            probes.parse_sysbench_cpu("no useful data here")

    def test_stream_gbps(self):
        s = probes.parse_stream(STREAM)
        self.assertAlmostEqual(s["triad"], 13.5)
        self.assertAlmostEqual(s["copy"], 12.0)

    def test_fio(self):
        f = probes.parse_fio(FIO)
        self.assertAlmostEqual(f["read_iops"], 5000.0)
        self.assertAlmostEqual(f["write_iops"], 100.0)
        self.assertAlmostEqual(f["read_bw_kbps"], 20000.0)

    def test_iperf3(self):
        n = probes.parse_iperf3(IPERF)
        self.assertAlmostEqual(n["received_bps"], 9.4e9)
        self.assertAlmostEqual(n["sent_bps"], 9.45e9)

    def test_ping_linux(self):
        out = ("2 packets transmitted, 2 received, 0% packet loss, time 1001ms\n"
               "rtt min/avg/max/mdev = 0.298/0.377/0.512/0.061 ms\n")
        r = probes.parse_ping(out)
        self.assertAlmostEqual(r["min_ms"], 0.298)
        self.assertAlmostEqual(r["avg_ms"], 0.377)
        self.assertAlmostEqual(r["max_ms"], 0.512)

    def test_ping_bsd_three_field(self):
        out = "round-trip min/avg/max = 0.1/0.2/0.3 ms\n"
        r = probes.parse_ping(out)
        self.assertAlmostEqual(r["min_ms"], 0.1)
        self.assertAlmostEqual(r["max_ms"], 0.3)

    def test_ping_missing(self):
        with self.assertRaises(ValueError):
            probes.parse_ping("no rtt summary here")


if __name__ == "__main__":
    unittest.main()
