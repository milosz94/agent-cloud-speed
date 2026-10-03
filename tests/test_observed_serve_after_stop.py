"""tools/instrument_change_scenarios.observed_serve_after_stop: the agent's own first sight of the app after an early stop.

Five published Azure runs had their clock stopped by the original predicate on the platform's stopped-site 403.
The detector reads, from the deploy transcript, the first tool result after that stop which is a check of the app's
own host with a line carrying a 200 that is not the stopped-site page. The synthetic cases pin the rules; the record
case pins the five published values (Part 5, section 5) when the published tree is present.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

HOST = "app-example.azurewebsites.net"


def _row(t, kind, block):
    return {"type": kind, "timestamp": "2026-09-01T00:%02d:%02d.000Z" % (t // 60, t % 60),
            "message": {"content": [block]}}


def _call(t, tid, cmd):
    return _row(t, "assistant", {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}})


def _result(t, tid, out):
    return _row(t, "user", {"type": "tool_result", "tool_use_id": tid, "content": out})


def _rec(makespan):
    return {"split": {"makespan_s": makespan}, "url": "https://" + HOST}


class ObservedServe(unittest.TestCase):
    def setUp(self):
        import instrument_change_scenarios as ics
        self.f = ics.observed_serve_after_stop

    def test_stopped_site_page_and_failures_do_not_count_but_a_later_200_line_does(self):
        rows = [_call(0, "a", "echo start"), _result(1, "a", "ok"),
                _call(20, "b", "curl https://%s/api/heartbeat" % HOST),
                # the stopped-site page may carry a 200 token in its markup; it must still not count, and neither may a
                # 200 inside a longer number (1200, 12000)
                _result(30, "b", "hb=200 <title>Web App - Unavailable</title><style>#f{width:200px}</style>\n"
                                 "hb=503 time=1.200s size=12000\nhb=000"),
                _call(40, "c", "curl https://%s/api/heartbeat" % HOST),
                _result(55, "c", "hb=403 <title>Web App - Unavailable</title>\nhb=200 {\"ok\":true}")]
        stop, seen = self.f(_rec(10.0), rows)          # stop = first event (t=0) + 10 s
        self.assertAlmostEqual(seen - stop, 45.0)      # the result at t=55, not the failures at t=30

    def test_a_200_from_another_host_or_before_the_stop_does_not_count(self):
        rows = [_call(0, "a", "curl https://%s/" % HOST), _result(5, "a", "200"),   # before the stop
                _call(20, "b", "curl https://other.example/"), _result(25, "b", "200"),
                _call(30, "c", "az webapp list"), _result(35, "c", "\"timeout\": 1200")]
        stop, seen = self.f(_rec(10.0), rows)
        self.assertIsNone(seen)

    def test_published_five(self):
        import paper_tables as pt
        from acspeed import transcript as T
        want = {"bef9e3ac": 872.5, "4e6d51a7": 119.8, "ca89af8f": 765.7, "432c2ee4": 111.3, "4a7318ab": 558.7}
        lead_want = {"bef9e3ac": 8.2, "4e6d51a7": 3.6, "ca89af8f": 7.9, "432c2ee4": 3.7, "4a7318ab": 39.5}
        import instrument_change_scenarios as ics
        got, lead = {}, {}
        try:
            for key, sfx, _name in pt.TIERS:
                for u, r in zip(pt.published_uuids("azure", key), pt.published_records("azure", sfx, key)):
                    if str(r["serving"].get("http_code")) == "403":
                        rows = T._load_rows(os.path.join(pt.RESULTS, "azure", "azure-" + key, "sessions", u + ".jsonl"))
                        stop, seen = self.f(r, rows)
                        got[u[:8]] = round(seen - stop, 1)
                        lead[u[:8]] = round(ics.own_stop_lead_s(r, rows), 1)
        except (OSError, KeyError) as e:                # published tree absent
            self.skipTest("published records not available: %s" % e)
        self.assertEqual(got, want)
        self.assertEqual(lead, lead_want)


if __name__ == "__main__":
    unittest.main()
