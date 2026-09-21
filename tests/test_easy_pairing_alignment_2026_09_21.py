"""The run-for-run alignment the paired cost bootstrap depends on, and the four ways it can break.

``paper_tables._suite_replicates`` pairs a replicate's cost draw to its Easy-cell makespan draw BY
INDEX, so position i of ``Ms`` and position i of ``cost`` must be the same run. If they shift, every
replicate pairs one run's time with another run's cost, and the result is wrong in a way that looks
entirely normal.

Nothing else in this repository detects that. Pairing each makespan with a randomly relabelled run's
cost was measured at 7.797 percent against the correct 7.739, well inside the bootstrap's own Monte
Carlo noise at B = 10,000, so the paired-versus-unpaired sensitivity Part 5 reports CANNOT see a
mispairing. A length guard cannot either: two lists of equal length can still be in different orders.

The live hazard is concrete rather than hypothetical. ``cell()`` builds ``Ms`` with a falsy filter and
``cost`` without one, so one unmeasured makespan shifts every index after it. Today all nine cells
have one priced run per makespan and nothing is dropped, which is exactly why the guard has to be
mechanical: it protects a property that currently holds and would be silent if it stopped holding.

Each test below breaks the data one way and asserts the checker fails. A checker that never fails is
worse than no checker at all.
"""
import copy
import os
import unittest

from tools import paper_tables as pt


def _cells():
    return pt.all_cells()


def _easy(cells, cloud):
    return next(c for c in cells if c["cloud"] == cloud and c["tier"] == "Easy")


class EasyPairingAlignment(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.cells = _cells()
        except (SystemExit, OSError) as exc:
            raise unittest.SkipTest(f"no results tree to check against: {exc}")

    def test_published_data_is_aligned(self):
        """The positive case. Every Easy cell pairs run-for-run as published."""
        self.assertEqual(pt.check_easy_pairing_alignment(self.cells), [])

    def test_dropped_makespan_is_caught(self):
        """The live hazard: Ms filters falsy values, cost does not, so a drop shifts one list."""
        cells = copy.deepcopy(self.cells)
        _easy(cells, "azure")["Ms"] = _easy(cells, "azure")["Ms"][1:]
        problems = pt.check_easy_pairing_alignment(cells)
        self.assertTrue(problems)
        self.assertIn("makespans", problems[0])

    def test_reordered_makespans_are_caught_at_equal_length(self):
        """Equal length is not alignment. Two swapped runs must still fail."""
        cells = copy.deepcopy(self.cells)
        ms = _easy(cells, "azure")["Ms"]
        ms[0], ms[3] = ms[3], ms[0]
        problems = pt.check_easy_pairing_alignment(cells)
        self.assertTrue(problems)
        self.assertIn("position 0", problems[0])

    def test_rotated_cost_list_is_caught(self):
        """The mispairing the published paired-versus-unpaired sensitivity cannot see."""
        cells = copy.deepcopy(self.cells)
        cost = _easy(cells, "azure")["cost"]
        _easy(cells, "azure")["cost"] = cost[1:] + cost[:1]
        problems = pt.check_easy_pairing_alignment(cells)
        self.assertTrue(problems)
        self.assertIn("cost", problems[0])

    def test_every_easy_cell_is_covered_not_just_the_first(self):
        """A break on GCP must fail too, or the check only ever looks at one cell."""
        cells = copy.deepcopy(self.cells)
        cost = _easy(cells, "gcp")["cost"]
        cost[2], cost[5] = cost[5], cost[2]
        problems = pt.check_easy_pairing_alignment(cells)
        self.assertTrue(problems)
        self.assertTrue(any(p.startswith("gcp") for p in problems))

    # ---------------------------------------------------------------------------------------
    # The assertions below read the RESULTS tree rather than the cell dict, so they are driven by
    # substituting what published_uuids / published_records return. That is the assertion logic
    # under test, fed exactly the shapes the real functions produce. The suite's remaining blind
    # spot, named by the audit that prompted these: no test mutates an actual record file.
    # ---------------------------------------------------------------------------------------

    def _with_sources(self, uuids, recs):
        """Run the check against one Azure Easy cell with published_uuids/records substituted."""
        cells = copy.deepcopy(self.cells)
        cell = _easy(cells, "azure")
        cell["Ms"] = [pt.task_M(r) for r in recs]
        cell["cost"] = [(r.get("cost_run_rate") or {}) for r in recs]
        cells = [c for c in cells if not (c["cloud"] == "azure" and c["tier"] != "Easy")]
        u, r = pt.published_uuids, pt.published_records
        pt.published_uuids = lambda cl, k: uuids if cl == "azure" else u(cl, k)
        pt.published_records = lambda cl, sx, k: recs if cl == "azure" else r(cl, sx, k)
        try:
            return pt.check_easy_pairing_alignment(cells)
        finally:
            pt.published_uuids, pt.published_records = u, r

    def test_a_session_published_twice_is_caught(self):
        """One run supplying two published rows is a pseudo-replicate, not a repetition."""
        recs = pt.published_records("azure", "", "easy")
        uuids = pt.published_uuids("azure", "easy")
        problems = self._with_sources(uuids[:-1] + [uuids[0]], recs)
        self.assertTrue(any("published twice" in p for p in problems), problems)

    def test_two_rows_resolving_to_one_record_is_caught(self):
        """The failure the old UUID assertion claimed to catch, and could not."""
        recs = pt.published_records("azure", "", "easy")
        problems = self._with_sources(pt.published_uuids("azure", "easy"), recs[:-1] + [recs[0]])
        self.assertTrue(any("SAME run record" in p for p in problems), problems)

    def test_an_unpriced_position_is_caught(self):
        """A makespan with no hourly rate beside it would pair against nothing."""
        recs = [dict(r) for r in pt.published_records("azure", "", "easy")]
        recs[4]["cost_run_rate"] = {}
        problems = self._with_sources(pt.published_uuids("azure", "easy"), recs)
        self.assertTrue(any("position 4: no hourly rate" in p for p in problems), problems)

    def test_a_vacuous_run_is_reported_not_passed(self):
        """check(no Easy cells) used to return [], which reads as 'verified' and proves nothing."""
        problems = pt.check_easy_pairing_alignment([c for c in self.cells if c["tier"] != "Easy"])
        self.assertTrue(any("vacuously" in p for p in problems), problems)
        self.assertEqual(pt.check_easy_pairing_alignment([])[0].count("vacuously"), 1)

    def test_the_bootstrap_refuses_rather_than_warns(self):
        """Detecting the break is not enough: the paired bootstrap must not produce a number."""
        cells = copy.deepcopy(self.cells)
        cost = _easy(cells, "azure")["cost"]
        _easy(cells, "azure")["cost"] = cost[1:] + cost[:1]
        with self.assertRaises(SystemExit):
            pt._suite_replicates(cells, paired=True)


class FrontierExact(unittest.TestCase):
    """Azure's frontier frequency is enumerated, not sampled. Its two preconditions must refuse.

    The reduction that makes enumeration possible is a property of THIS wave, not of the estimator:
    AWS's cheapest Easy run happens to be dearer than Azure's dearest, and GCP happened to be faster
    than Azure in every replicate. If a later wave breaks either, the closed form is simply wrong,
    and it must fail loudly rather than keep printing a number.
    """

    @classmethod
    def setUpClass(cls):
        try:
            cls.cells = pt.all_cells()
        except (SystemExit, OSError) as exc:
            raise unittest.SkipTest(f"no results tree: {exc}")

    def test_the_exact_value_is_what_the_paper_prints(self):
        r = pt.frontier_exact(self.cells, replicates=20000)
        self.assertEqual(f"{r['pct']:.6f}", "7.768696")
        self.assertEqual(f"{r['pct']:.2f}", "7.77")
        self.assertEqual(r["outcomes"], 26026)

    def test_the_law_of_each_resampled_mean_sums_to_one(self):
        for cloud in ("gcp", "azure"):
            cell = next(c for c in self.cells if c["cloud"] == cloud and c["tier"] == "Easy")
            rates = [pt._hourly(x) for x in cell["cost"]]
            self.assertEqual(sum(pt._mean_law(rates).values()), 1)

    def test_the_exact_value_agrees_with_the_bootstrap(self):
        """Not a proof, a smoke alarm: the two must not disagree by more than sampling allows."""
        r = pt.frontier_exact(self.cells, replicates=20000)
        self.assertLess(abs(r["pct"] - r["bootstrap_pct"]), 1.0)

    def test_condition_A_refuses_when_the_dearer_cloud_could_be_cheaper(self):
        """If AWS could undercut Azure it could dominate Azure, and cost alone no longer decides."""
        cells = copy.deepcopy(self.cells)
        aws = next(c for c in cells if c["cloud"] == "aws" and c["tier"] == "Easy")
        aws["cost"] = [{"traffic_estimate": {"low": 0.1}} for _ in aws["cost"]]
        with self.assertRaises(SystemExit) as cm:
            pt.frontier_exact(cells, replicates=2000)
        self.assertIn("condition A fails", str(cm.exception))

    def test_condition_B_refuses_when_the_faster_cloud_is_ever_slower(self):
        """If GCP can be slower than Azure, its dominance stops turning on cost alone."""
        cells = copy.deepcopy(self.cells)
        for c in cells:
            if c["cloud"] == "gcp":
                c["Ms"] = [m * 10 for m in c["Ms"]]
        with self.assertRaises(SystemExit) as cm:
            pt.frontier_exact(cells, replicates=2000)
        self.assertIn("condition B fails", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
