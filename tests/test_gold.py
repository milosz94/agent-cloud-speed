"""Part 3 gold instantiated from run history (acspeed/gold.py)."""
import unittest

from acspeed import gold


def _rec(run, platform, makespan, served=True):
    return {"run": run, "split": {"critical_platform_s": platform, "makespan_s": makespan},
            "time_to_serving_s": makespan if served else None}


class TestPart3Provision(unittest.TestCase):
    def test_bracket_and_split_from_history(self):
        # 3 runs: platforms [120, 100, 110], makespans [200, 150, 180].
        # F_C = min platform = 100; best-achieved = min makespan = 150.
        recs = [_rec(1, 120.0, 200.0), _rec(2, 100.0, 150.0), _rec(3, 110.0, 180.0)]
        out = gold.part3_provision(recs)
        self.assertEqual(out["F_C_s"], 100.0)
        self.assertEqual(out["best_achieved_s"], 150.0)
        self.assertEqual(out["bracket_low_s"], 100.0)
        self.assertEqual(out["bracket_high_s"], 150.0)
        self.assertEqual(out["bracket_width_s"], 50.0)
        self.assertAlmostEqual(out["bracket_ratio"], 1.5)
        self.assertEqual(out["n_runs"], 3)
        # run 1: makespan 200 over the F_C floor 100 -> floor_ratio 2.0, all excess is execution.
        r1 = next(p for p in out["per_run"] if p["run"] == 1)
        self.assertAlmostEqual(r1["floor_ratio"], 2.0)
        self.assertAlmostEqual(r1["execution_excess_s"], 100.0)   # 200 - F_C(100)
        self.assertEqual(r1["selection_excess_s"], 0.0)           # 1-op suite: no selection
        self.assertTrue(r1["identity_holds"])

    def test_never_served_run_is_excluded_from_floor_and_frontier(self):
        # A FAILURE-never-served run carries a low makespan from its failed attempt (platform 0.3 +
        # agent 0.8). It must NOT set the floor or the best-achieved frontier (paper: valid traces that
        # reached the goal). Here it would spuriously push F_C to 0.3 and best to 1.1 if not filtered.
        recs = [_rec(1, 120.0, 200.0), _rec(2, 100.0, 150.0),
                _rec(99, 0.3, 1.1, served=False)]     # never served
        out = gold.part3_provision(recs)
        self.assertEqual(out["n_runs"], 2)             # the failed run is gone
        self.assertEqual(out["F_C_s"], 100.0)          # floor from served runs only, not 0.3
        self.assertEqual(out["best_achieved_s"], 150.0)  # frontier from served runs only, not 1.1

    def test_first_poll_on_a_4xx_is_excluded(self):
        # An EDGE answered before the app did (a container-service hostname 404s the moment DNS exists).
        # t1 is false-early and its clipped low platform must not define the global-min floor.
        suspect = _rec(7, 5.0, 8.0)                        # low platform, would drag F_C to 5.0
        suspect["serving"] = {"served_on_first_poll": True, "http_code": 404}
        suspect["run_token"] = "acsdeadbeef"
        suspect["url"] = "https://umami-acsdeadbeef.cs.amazonlightsail.com"
        recs = [_rec(1, 120.0, 200.0), _rec(2, 100.0, 150.0), suspect]
        out = gold.part3_provision(recs)
        self.assertEqual(out["n_runs"], 2)
        self.assertEqual(out["n_excluded_suspect"], 1)
        self.assertEqual(out["F_C_s"], 100.0)             # not 5.0

    def test_first_poll_on_a_200_with_its_own_token_is_ADMISSIBLE(self):
        # The deploy call did not hand back the URL until the service was already serving (Cloud Run
        # prints the URL after the revision is live), so the poller could not observe a not-yet-serving
        # state however early it started. t1 is an honest UPPER bound and the run is a valid trace.
        # Excluding it would drop every serverless-container run from the floor and the frontier.
        ok = _rec(7, 90.0, 140.0)
        ok["serving"] = {"served_on_first_poll": True, "http_code": 200}
        ok["run_token"] = "acs7ef2cc38"
        ok["url"] = "https://umami-acs7ef2cc38-299813327652.us-central1.run.app"
        recs = [_rec(1, 120.0, 200.0), _rec(2, 100.0, 150.0), ok]
        out = gold.part3_provision(recs)
        self.assertEqual(out["n_runs"], 3)
        self.assertEqual(out["n_excluded_suspect"], 0)
        self.assertEqual(out["F_C_s"], 90.0)              # it legitimately sets the floor
        self.assertEqual(out["best_achieved_s"], 140.0)   # and the frontier

    def test_first_poll_without_its_own_token_in_the_url_stays_suspect(self):
        # A LEFTOVER deployment from an earlier run would also answer on the first poll. Per-run resource
        # naming rules that out; when the token is absent (custom domain, proxy) it cannot be ruled out.
        suspect = _rec(7, 5.0, 8.0)
        suspect["serving"] = {"served_on_first_poll": True, "http_code": 200}
        suspect["run_token"] = "acsdeadbeef"
        suspect["url"] = "https://analytics.example.com"   # no run token
        recs = [_rec(1, 120.0, 200.0), _rec(2, 100.0, 150.0), suspect]
        out = gold.part3_provision(recs)
        self.assertEqual(out["n_runs"], 2)
        self.assertEqual(out["n_excluded_suspect"], 1)
        self.assertEqual(out["F_C_s"], 100.0)

    def test_first_poll_with_an_unreadable_code_stays_suspect(self):
        # Cannot tell what stopped the clock -> stay conservative rather than admit a possible edge hit.
        suspect = _rec(7, 5.0, 8.0)
        suspect["serving"] = {"served_on_first_poll": True, "http_code": None}
        recs = [_rec(1, 120.0, 200.0), _rec(2, 100.0, 150.0), suspect]
        out = gold.part3_provision(recs)
        self.assertEqual(out["n_excluded_suspect"], 1)
        self.assertEqual(out["F_C_s"], 100.0)

    def test_falls_back_to_time_to_serving(self):
        # makespan_s missing -> use time_to_serving_s
        recs = [{"run": 1, "split": {"critical_platform_s": 90.0}, "time_to_serving_s": 140.0}]
        out = gold.part3_provision(recs)
        self.assertEqual(out["F_C_s"], 90.0)
        self.assertEqual(out["best_achieved_s"], 140.0)
        self.assertEqual(out["n_runs"], 1)

    def test_none_when_no_usable_split(self):
        self.assertIsNone(gold.part3_provision([]))
        self.assertIsNone(gold.part3_provision([{"run": 1}]))                       # no split
        self.assertIsNone(gold.part3_provision([{"run": 1, "split": {"makespan_s": 10.0}}]))  # no platform


if __name__ == "__main__":
    unittest.main()
