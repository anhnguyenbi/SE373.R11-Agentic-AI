import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "b1_compact"), str(ROOT / "b2_code_mode"), str(ROOT.parent)]

from code_runtime import MAX_STDOUT, run_python  # noqa: E402
from compaction import CompactingSession, extract_state, rule_summarizer  # noqa: E402
from log_fixture import contains_raw_log, generate_logs  # noqa: E402
from session_fixture import build_turns  # noqa: E402


class CompactionTest(unittest.TestCase):
    def make(self, tmp, budget=24000):
        return CompactingSession("sys", rule_summarizer, budget_tokens=budget, archive_path=Path(tmp) / "a.jsonl")

    def test_no_compaction_under_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.make(tmp, budget=10**9)
            for t in build_turns():
                s.add_turn(t)
            self.assertFalse(s.maybe_compact())
            self.assertIsNone(s.checkpoint)

    def test_compaction_keeps_recent_turns_and_archives_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.make(tmp)
            turns = build_turns()
            for t in turns:
                s.add_turn(t)
                s.maybe_compact()
            self.assertGreater(len(s.events), 0)
            kept = len(s.turns)
            self.assertEqual(s.turns, turns[-kept:])  # các lượt còn lại là các lượt mới nhất, nguyên văn
            archived = (Path(tmp) / "a.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(archived), sum(len(t) for t in turns[:-kept]))
            self.assertLess(s.context_tokens(), 0.7 * 24000)

    def test_context_keeps_tool_call_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.make(tmp)
            for t in build_turns():
                s.add_turn(t)
                s.maybe_compact()
            ctx = s.context()
            call_ids = {c["id"] for m in ctx for c in m.get("tool_calls") or []}
            tool_ids = {m["tool_call_id"] for m in ctx if m["role"] == "tool"}
            self.assertEqual(call_ids, tool_ids)

    def test_checkpoint_merges_across_compactions(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self.make(tmp)
            for t in build_turns():
                s.add_turn(t)
                s.maybe_compact()
            s.compact()  # ép compact lần cuối để mọi lượt cũ đi vào checkpoint
            self.assertGreaterEqual(len(s.events), 3)
            self.assertEqual(len(s.turns), 2)
            cp = s.checkpoint
            self.assertEqual(len(cp["da_thu"]), 3)  # nginx, gunicorn, memory limit
            self.assertTrue(any("public" in r for r in cp["rang_buoc"]))
            self.assertIn("MemoryError", cp["loi_dang_dieu_tra"])
            self.assertFalse(contains_raw_log(json.dumps(cp)))

    def test_extract_state_ignores_noise(self):
        state = extract_state([{"role": "tool", "content": "line a\nline b"}])
        self.assertEqual(state["bang_chung"], [])


class CodeRuntimeTest(unittest.TestCase):
    def test_environment_is_cleared(self):
        os.environ["OPENAI_API_KEY"] = "sk-secret-test"
        try:
            out = run_python("import os; print(os.environ.get('OPENAI_API_KEY'))", Path.cwd())
        finally:
            del os.environ["OPENAI_API_KEY"]
        self.assertIn("None", out)
        self.assertNotIn("sk-secret-test", out)

    def test_output_is_truncated(self):
        out = run_python("print('x' * 10000)", Path.cwd())
        self.assertLess(len(out), MAX_STDOUT + 200)
        self.assertIn("cắt bớt", out)

    def test_timeout(self):
        import code_runtime
        old = code_runtime.TIMEOUT_S
        code_runtime.TIMEOUT_S = 1
        try:
            self.assertIn("timeout", run_python("while True: pass", Path.cwd()))
        finally:
            code_runtime.TIMEOUT_S = old


class LogFixtureTest(unittest.TestCase):
    def test_expected_matches_independent_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = generate_logs(Path(tmp) / "logs", 120)
            counts = {}
            for p in (Path(tmp) / "logs").glob("*.log"):
                for line in p.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if parts[2] == "ERROR":
                        code = parts[3].split("=")[1]
                        counts[code] = counts.get(code, 0) + 1
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
            self.assertEqual([[c, n] for c, n in top], expected["top3"])


class DemoTest(unittest.TestCase):
    def run_demo(self, script):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, "-B", str(script), "--out", tmp], capture_output=True,
                                  text=True, encoding="utf-8")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads((Path(tmp) / "report.json").read_text(encoding="utf-8"))

    def test_b1_offline(self):
        report = self.run_demo(ROOT / "b1_compact" / "demo_b1.py")
        self.assertTrue(all(report["runs"]["compacted"]["facts"].values()))
        self.assertFalse(all(report["runs"]["truncated"]["facts"].values()))

    def test_b2_offline(self):
        report = self.run_demo(ROOT / "b2_code_mode" / "demo_b2.py")
        self.assertTrue(report["runs"]["raw"]["raw_log_in_context"])
        self.assertFalse(report["runs"]["code"]["raw_log_in_context"])


if __name__ == "__main__":
    unittest.main()
