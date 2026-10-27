import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memory_store import ACTIVE, SUPERSEDED, MemoryStore  # noqa: E402


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memory.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_persists_across_instances(self):
        MemoryStore(self.path).remember("api.json_case", "snake_case", "s1")
        reloaded = MemoryStore(self.path)
        self.assertEqual(reloaded.current("api.json_case").content, "snake_case")

    def test_change_supersedes_old_entry_and_keeps_history(self):
        store = MemoryStore(self.path)
        old = store.remember("api.json_case", "snake_case", "s1")
        new = store.remember("api.json_case", "camelCase", "s3")
        store = MemoryStore(self.path)
        self.assertEqual(store.get(old.id).status, SUPERSEDED)
        self.assertEqual(store.get(old.id).superseded_by, new.id)
        self.assertEqual(store.get(new.id).status, ACTIVE)
        self.assertEqual(len(store.history("api.json_case")), 2)

    def test_context_only_contains_active_conventions(self):
        store = MemoryStore(self.path)
        store.remember("api.json_case", "snake_case", "s1")
        store.remember("api.json_case", "camelCase", "s3")
        context = store.render_for_context()
        self.assertIn("camelCase", context)
        self.assertNotIn("snake_case", context)

    def test_same_content_is_not_duplicated(self):
        store = MemoryStore(self.path)
        store.remember("git.commit_style", "Conventional Commits", "s1")
        store.remember("git.commit_style", "Conventional Commits", "s2")
        self.assertEqual(len(store.entries), 1)

    def test_replaces_id_must_point_to_active_entry(self):
        store = MemoryStore(self.path)
        store.remember("a.b", "x", "s1")
        store.remember("a.b", "y", "s2")
        with self.assertRaises(ValueError):
            store.remember("a.b", "z", "s3", replaces_id="m1")  # m1 đã bị thay

    def test_replaces_id_can_rename_topic(self):
        store = MemoryStore(self.path)
        old = store.remember("json", "snake_case", "s1")
        new = store.remember("api.json_case", "camelCase", "s2", replaces_id=old.id)
        self.assertEqual(store.get(old.id).status, SUPERSEDED)
        self.assertEqual(new.supersedes, old.id)

    def test_retire(self):
        store = MemoryStore(self.path)
        store.remember("a.b", "x", "s1")
        store.retire("a.b")
        self.assertEqual(store.active(), [])


class DemoTest(unittest.TestCase):
    def test_offline_demo_passes_all_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, "-B", str(HERE / "demo.py"), "--out", tmp],
                                  capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertNotIn("FAIL", proc.stdout)


if __name__ == "__main__":
    unittest.main()
