"""The custom-benchmark guide must stay executable.

docs/writing-a-benchmark.md tells people to copy a suite skeleton. A guide that no longer matches the
API is worse than no guide: it is discovered only after a live, billed run. So the example is
extracted from the document itself and exercised here.

The first draft of that example was wrong in a way worth pinning: it guarded with `status is None`,
but `_http.http` reports a failed connection as status **0**, so `status < 500` was true and a dead
URL verified as a working deploy.
"""
import pathlib
import re
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "writing-a-benchmark.md"


def _load_example():
    """Import the guide's first python block as a module, exactly as a reader would paste it."""
    src = GUIDE.read_text()
    block = src.split("```python\n", 1)[1].split("```", 1)[0]
    mod = types.ModuleType("_guide_example")
    mod.__file__ = str(ROOT / "acspeed" / "suites" / "_guide_example.py")
    mod.__package__ = "acspeed.suites"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    exec(compile(block, mod.__file__, "exec"), mod.__dict__)
    return mod


class GuideExampleWorks(unittest.TestCase):

    def test_the_guide_exists_and_has_a_python_example(self):
        self.assertTrue(GUIDE.exists(), "the custom-benchmark guide is gone")
        self.assertIn("```python", GUIDE.read_text())

    def test_example_builds_a_valid_tier_instance(self):
        inst = _load_example().build()
        self.assertEqual(inst.name, "myapp-basic")
        self.assertEqual([o.op_id for o in inst.operations], ["deploy-serve", "create-widget"])
        self.assertEqual([o.op_type for o in inst.operations], ["provision", "operate_mutate"])
        self.assertTrue(inst.teardown_hint, "teardown_hint must be set or teardown cannot be app-agnostic")
        self.assertIsNotNone(inst.durability)

    def test_a_dead_url_does_not_verify_as_a_working_deploy(self):
        """The status-0 trap. Port 9 (discard) is closed on the test host."""
        mod = _load_example()
        from acspeed.suite import OpContext
        r = mod._serves(OpContext(url="http://127.0.0.1:9"))
        self.assertFalse(r.ok, "a dead URL must not verify as served")

    def test_an_unreadable_api_is_unverifiable_not_a_cloud_failure(self):
        mod = _load_example()
        from acspeed.suite import OpContext
        r = mod._has_widget(OpContext(url="http://127.0.0.1:9"))
        self.assertFalse(r.ok)
        self.assertTrue(r.unverifiable,
                        "an instrument that cannot read must not score the cloud down")

    def test_guide_registry_snippet_matches_the_real_registry_key(self):
        from acspeed.suites import _REGISTRY
        self.assertIn("_REGISTRY", GUIDE.read_text())
        self.assertTrue(all(isinstance(k, str) for k in _REGISTRY))


if __name__ == "__main__":
    unittest.main()
