"""分层 TM：永久权威库只读，运行期 TM 按批隔离。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import batch
from qa_checks import run_qa


class TmLayersTest(unittest.TestCase):
    def test_permanent_and_runtime_matches_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            permanent = tmp / "permanent.json"
            runtime_dir = tmp / "runtime"
            permanent.write_text(json.dumps({"entries": [{
                "source": "こんにちは", "target": "权威译文", "context": "", "file": "master"
            }]}, ensure_ascii=False), encoding="utf-8")
            runtime_dir.mkdir()
            runtime = runtime_dir / "_batch_001.json"
            runtime.write_text(json.dumps({"entries": [{
                "source": "こんにちは", "target": "临时译文", "context": "", "file": "batch-1"
            }]}, ensure_ascii=False), encoding="utf-8")

            work = tmp / "working.json"
            work.write_text(json.dumps({"entries": [{"id": "1", "source": "こんにちは"}]}, ensure_ascii=False), encoding="utf-8")
            state = {
                "project_id": "layered",
                "tm_permanent_path": str(permanent),
                "tm_runtime_dir": str(runtime_dir),
                "tm_runtime_files": [str(runtime)],
            }
            batch._enrich_working_json(work, state)
            entry = json.loads(work.read_text(encoding="utf-8"))["entries"][0]

            self.assertEqual(entry["tm_matches"][0]["target"], "权威译文")
            self.assertEqual(entry["tm_matches"][0]["tm_scope"], "permanent")
            self.assertEqual(entry["runtime_tm_matches"][0]["target"], "临时译文")
            self.assertEqual(entry["runtime_tm_matches"][0]["tm_scope"], "runtime")

            qa = run_qa(
                [{"id": "1", "target": "临时译文"}],
                [entry],
            )
            self.assertEqual(qa["findings"][0]["tm_scope"], "permanent")

    def test_runtime_match_is_used_when_permanent_has_no_exact_match(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            work = tmp / "working.json"
            runtime = tmp / "_batch_001.json"
            runtime.write_text(json.dumps({"entries": [{
                "source": "おはよう", "target": "早上好", "context": "", "file": "batch-1"
            }]}, ensure_ascii=False), encoding="utf-8")
            work.write_text(json.dumps({"entries": [{"id": "1", "source": "おはよう"}]}, ensure_ascii=False), encoding="utf-8")
            batch._enrich_working_json(work, {
                "project_id": "runtime-only",
                "tm_permanent_path": str(tmp / "missing.json"),
                "tm_runtime_dir": str(tmp),
                "tm_runtime_files": [str(runtime)],
            })
            entry = json.loads(work.read_text(encoding="utf-8"))["entries"][0]
            qa = run_qa(
                [{"id": "1", "target": "早安"}],
                [entry],
            )
            self.assertEqual(qa["findings"][0]["tm_scope"], "runtime")


if __name__ == "__main__":
    unittest.main()
