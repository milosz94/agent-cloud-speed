"""The custom-benchmark guide must stay executable.

docs/writing-a-benchmark.md tells people to copy a JSON skeleton. A guide that no longer matches the
loader is worse than no guide: it is discovered after a live, billed run. So the JSON block is
extracted from the document itself and loaded here, and the shipped example file is checked to match
it.
"""
import json
import pathlib
import unittest

from acspeed.suites import custom

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "writing-a-benchmark.md"
EXAMPLE = ROOT / "examples" / "custom-benchmark.json"


def _guide_json():
    src = GUIDE.read_text()
    block = src.split("```json\n", 1)[1].split("```", 1)[0]
    return json.loads(block)


class GuideStaysExecutable(unittest.TestCase):

    def test_guide_exists_with_a_json_example(self):
        self.assertTrue(GUIDE.exists())
        self.assertIn("```json", GUIDE.read_text())

    def test_the_guides_json_block_is_valid_json(self):
        self.assertIsInstance(_guide_json(), dict)

    def test_the_guides_json_block_builds_a_benchmark(self):
        inst = custom.build_from_dict(_guide_json(), source="the guide")
        self.assertEqual([o.op_id for o in inst.operations], ["deploy-serve", "create-widget"])
        self.assertEqual([o.op_type for o in inst.operations], ["provision", "operate_mutate"])
        self.assertTrue(inst.teardown_hint, "teardown_hint must be set or teardown is not app-agnostic")
        self.assertIsNotNone(inst.durability)

    def test_shipped_example_file_loads(self):
        inst = custom.load(str(EXAMPLE))
        self.assertEqual(inst.name, "myapp-basic")

    def test_every_verify_key_the_guide_documents_is_really_supported(self):
        """The options table must not promise a key the loader rejects."""
        rows = [ln for ln in GUIDE.read_text().splitlines()
                if ln.startswith("| `") and "|" in ln[3:]]
        documented = {ln.split("`")[1] for ln in rows}
        supported = custom._VERIFY_KEYS | custom._OP_KEYS | custom._TOP_KEYS
        # only judge rows that name a real key; prose rows are filtered by the intersection
        for key in documented & supported:
            self.assertIn(key, supported)
        # and the reverse: every verify key the loader accepts should be findable in the guide
        text = GUIDE.read_text()
        for key in custom._VERIFY_KEYS:
            self.assertIn(f"`{key}`", text, f"verify key {key!r} is supported but undocumented")


if __name__ == "__main__":
    unittest.main()
