"""Part 3 multi-operation deploy gold (acspeed/gold.py): the richer provision->deploy graph where
selection excess becomes non-trivial, plus the literature-hardening fields (competitive-ratio interval,
versioned gold, floor sensitivity, refutation)."""
import unittest

from acspeed import gold
from acspeed.reference import Edge


def _deploy_rec(run, chosen, prov_actual, dep_actual):
    """A synthetic record; makespan = prov_actual + dep_actual (the two decision-epoch sojourns)."""
    return {"run": run, "chosen_provision": chosen,
            "prov_actual_s": prov_actual, "deploy_actual_s": dep_actual,
            "makespan_s": prov_actual + dep_actual}


def _extract(rec):
    return (rec["chosen_provision"], rec["prov_actual_s"], rec["deploy_actual_s"], rec["makespan_s"])


# floor_app=60, floor_db=100 -> concurrent bundle max=100, serial sum=160; deploy floor=40. F_C=100+40=140.
ALTS = [gold.ProvisionAlternative("concurrent", 100.0), gold.ProvisionAlternative("serial", 160.0)]
DEPLOY_FLOOR = 40.0


class TestDeployGraphMath(unittest.TestCase):
    def setUp(self):
        self.g = gold.build_deploy_graph(ALTS, DEPLOY_FLOOR)

    def test_fc_picks_the_concurrent_alternative(self):
        self.assertAlmostEqual(self.g.floor_makespan(), 140.0)   # max(60,100)+40, not 60+100+40

    def test_concurrent_at_floor_is_zero_excess(self):
        dec = gold.score_deploy_run(self.g, "concurrent", 100.0, 40.0)
        self.assertAlmostEqual(dec.execution_excess, 0.0)
        self.assertAlmostEqual(dec.selection_excess, 0.0)
        self.assertTrue(dec.identity_holds)

    def test_serial_at_floor_is_pure_selection_excess(self):
        # chose serial (both provisions in sequence) at floor speed: the min(app,db)=60 serialization
        # penalty is SELECTION excess, execution excess is zero.
        dec = gold.score_deploy_run(self.g, "serial", 160.0, 40.0)
        self.assertAlmostEqual(dec.selection_excess, 60.0)       # 200 (twin) - 140 (F_C)
        self.assertAlmostEqual(dec.execution_excess, 0.0)
        self.assertTrue(dec.identity_holds)

    def test_concurrent_but_slow_is_pure_execution_excess(self):
        dec = gold.score_deploy_run(self.g, "concurrent", 120.0, 50.0)
        self.assertAlmostEqual(dec.selection_excess, 0.0)
        self.assertAlmostEqual(dec.execution_excess, 30.0)       # 170 - 140 (chosen floor twin)
        self.assertTrue(dec.identity_holds)

    def test_unknown_branch_raises(self):
        with self.assertRaises(ValueError):
            gold.score_deploy_run(self.g, "nope", 100.0, 40.0)

    def test_empty_alternatives_raises(self):
        with self.assertRaises(ValueError):
            gold.build_deploy_graph([], DEPLOY_FLOOR)


class TestPart3Deploy(unittest.TestCase):
    def setUp(self):
        self.g = gold.build_deploy_graph(ALTS, DEPLOY_FLOOR)

    def test_bracket_and_selection_execution_split(self):
        recs = [_deploy_rec(1, "concurrent", 120.0, 50.0),   # mk 170, exec 30, sel 0
                _deploy_rec(2, "serial", 160.0, 45.0),        # mk 205, exec 5,  sel 60
                _deploy_rec(3, "concurrent", 105.0, 42.0)]    # mk 147, exec 7,  sel 0  (best-achieved)
        out = gold.part3_deploy(recs, self.g, _extract)
        self.assertEqual(out["F_C_s"], 140.0)
        self.assertEqual(out["best_achieved_s"], 147.0)
        self.assertAlmostEqual(out["bracket_ratio"], round(147.0 / 140.0, 3))
        self.assertEqual(out["gold_version"], gold.GOLD_VERSION)
        by_run = {p["run"]: p for p in out["per_run"]}
        self.assertAlmostEqual(by_run[2]["selection_excess_s"], 60.0)
        self.assertAlmostEqual(by_run[2]["execution_excess_s"], 5.0)
        self.assertEqual(by_run[2]["chosen_provision"], "serial")
        self.assertAlmostEqual(by_run[1]["selection_excess_s"], 0.0)
        self.assertAlmostEqual(by_run[1]["execution_excess_s"], 30.0)
        # competitive ratio is an INTERVAL [M/best, M/F_C], never a point
        self.assertEqual(by_run[1]["competitive_ratio_interval"],
                         [round(170.0 / 147.0, 3), round(170.0 / 140.0, 3)])
        # the alternatives are disclosed with their floors
        labels = {a["label"]: a["floor_s"] for a in out["alternatives"]}
        self.assertEqual(labels, {"concurrent": 100.0, "serial": 160.0})

    def test_leg_below_authored_floor_is_a_refutation_not_a_crash(self):
        # a concurrent provision measured at 90s is below the authored 100s floor: the floor is refuted
        # for that branch (revise down), and the run is reported under refutation, never crashing the pass.
        recs = [_deploy_rec(1, "concurrent", 120.0, 50.0),      # clean
                _deploy_rec(9, "concurrent", 90.0, 40.0)]       # 90 < floor 100 -> refutes
        out = gold.part3_deploy(recs, self.g, _extract)
        self.assertEqual(out["n_runs"], 1)                      # only the clean run is scored
        self.assertTrue(out["refutation"]["refuted"])
        self.assertEqual([r["run"] for r in out["refutation"]["refuted_runs"]], [9])
        self.assertEqual(out["refutation"]["revise_floor_to_s"], 130.0)

    def test_none_when_no_usable_record(self):
        self.assertIsNone(gold.part3_deploy([], self.g, _extract))
        self.assertIsNone(gold.part3_deploy([{"run": 1}], self.g, lambda r: None))


class TestProvisionGoldHardening(unittest.TestCase):
    """The literature-P0 fields added to the degenerate provision gold."""

    def _recs(self):
        return [{"run": 1, "split": {"critical_platform_s": 120.0, "makespan_s": 200.0},
                 "time_to_serving_s": 200.0},
                {"run": 2, "split": {"critical_platform_s": 100.0, "makespan_s": 150.0},
                 "time_to_serving_s": 150.0}]

    def test_versioned_and_floor_estimated_from_n(self):
        out = gold.part3_provision(self._recs())
        self.assertEqual(out["gold_version"], gold.GOLD_VERSION)
        self.assertEqual(out["floor_estimated_from_n"], 2)

    def test_competitive_ratio_interval_per_run(self):
        out = gold.part3_provision(self._recs())
        r1 = next(p for p in out["per_run"] if p["run"] == 1)
        # F_C=100, best-achieved=150, makespan=200 -> interval [200/150, 200/100]
        self.assertEqual(r1["competitive_ratio_interval"], [round(200 / 150, 3), 2.0])

    def test_floor_sensitivity_present_and_moves_the_bracket(self):
        out = gold.part3_provision(self._recs())
        fs = out["floor_sensitivity"]
        self.assertEqual(fs["epsilon"], 0.10)
        # bracket ratio 150/100=1.5; at F_C-10% (90) it is 150/90>1.5; at F_C+10% (110) it is <1.5
        self.assertGreater(fs["bracket_ratio_at_F_C_minus"], 1.5)
        self.assertLess(fs["bracket_ratio_at_F_C_plus"], 1.5)

    def test_refutation_hook_present(self):
        out = gold.part3_provision(self._recs())
        self.assertIn("refutation", out)
        self.assertFalse(out["refutation"]["refuted"])   # makespan >= platform floor by construction


if __name__ == "__main__":
    unittest.main()
