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

    def test_first_poll_flagged_run_is_excluded(self):
        # A run that served on the very FIRST poll is suspect (possible leftover deployment / early t1);
        # its clipped low platform must not define the global-min floor. Excluded, counted.
        suspect = _rec(7, 5.0, 8.0)                        # low platform, would drag F_C to 5.0
        suspect["serving"] = {"served_on_first_poll": True}
        recs = [_rec(1, 120.0, 200.0), _rec(2, 100.0, 150.0), suspect]
        out = gold.part3_provision(recs)
        self.assertEqual(out["n_runs"], 2)
        self.assertEqual(out["n_excluded_suspect"], 1)
        self.assertEqual(out["F_C_s"], 100.0)             # not 5.0

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
