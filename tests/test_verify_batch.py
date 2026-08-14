"""verify_batch 校验逻辑测试：项目策略例外与人工放行。"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_batch


class VerifyBatchTest(unittest.TestCase):
    STEM = "TestStem"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.data_dir = self.base / "data" / self.STEM
        self.exports_dir = self.base / "exports" / self.STEM
        self.data_dir.mkdir(parents=True)
        self.exports_dir.mkdir(parents=True)
        self.export_file = self.exports_dir / "_working.json"
        self.reviewed = self.exports_dir / "_batch_001_reviewed.json"
        self._old_script_dir = verify_batch.SCRIPT_DIR
        verify_batch.SCRIPT_DIR = self.base

    def tearDown(self):
        verify_batch.SCRIPT_DIR = self._old_script_dir
        self.tmp.cleanup()

    def _write_state(self, entries_count: int, policy: dict | None = None):
        state = {
            "stem": self.STEM,
            "current_batch": 0,
            "total_batches": 1,
            "batches": [[0, entries_count]],
            "export_file": str(self.export_file),
        }
        if policy is not None:
            policy_path = self.base / "validation_policy.json"
            policy_path.write_text(
                json.dumps(policy, ensure_ascii=False), encoding="utf-8"
            )
            state["validation_policy_path"] = str(policy_path)
        (self.data_dir / "batch_state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )

    def _run(self, allow_warnings: bool = False) -> str:
        sys.argv = ["verify_batch.py", "--stem", self.STEM] + (
            ["--allow-warnings"] if allow_warnings else []
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                verify_batch.main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)
        return out.getvalue()

    def test_project_policy_can_exempt_placeholder_tag_count(self):
        """项目策略可为指定占位符条目放宽标签比较。"""
        entries = [
            {
                "id": "1",
                "source": (
                    "（う……）<tag id='1' type='br' desc='换行'/>"
                    "<tag id='2' type='fmt' desc='⟨placeholder⟩'/>"
                    "<tag id='3' type='fmt' desc='⟨color=orange⟩'/>"
                ),
            },
            {
                "id": "2",
                "source": "あ<tag id='1' type='br' desc='换行'/>い",
            },
        ]
        self.export_file.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
        self.reviewed.write_text(
            json.dumps(
                [
                    {"id": "1", "target": "<tag id='2' type='fmt' desc='⟨placeholder⟩'/>"},
                    {"id": "2", "target": "あい"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._write_state(2, {
            "entry_overrides": {
                "1": {"tag_mode": "ignore"},
            }
        })

        out = self._run()
        self.assertNotIn("标签数与 source 不一致", out)
        self.assertIn("RESULT: PASS", out)

    def test_allow_warnings_flag(self):
        """--allow-warnings 时输出 warnings accepted。"""
        entries = [{"id": "1", "source": "あ"}]
        self.export_file.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
        self.reviewed.write_text(json.dumps({
            "entries": [{"id": "1", "target": "译文"}],
        }, ensure_ascii=False), encoding="utf-8")
        self._write_state(1)

        out = self._run(allow_warnings=True)
        self.assertIn("PASS (warnings accepted", out)

    def test_missing_id_is_fatal(self):
        """条数缺失仍为 FATAL，--allow-warnings 不能放行。"""
        entries = [{"id": "1", "source": "あ"}]
        self.export_file.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
        self.reviewed.write_text(
            json.dumps([], ensure_ascii=False), encoding="utf-8"
        )
        self._write_state(1)

        sys.argv = ["verify_batch.py", "--stem", self.STEM, "--allow-warnings"]
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stdout(io.StringIO()):
                verify_batch.main()
        self.assertNotEqual(cm.exception.code, 0)

    def test_bare_tag_in_target_is_fatal(self):
        """target 含非 <tag .../> 形式的裸标签（疑似照抄 TM 参考）→ FATAL。"""
        entries = [
            {"id": "1", "source": "あ<tag id='1' type='fmt' desc='⟨color=orange⟩'/>い"},
        ]
        self.export_file.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
        self.reviewed.write_text(
            json.dumps([{"id": "1", "target": "あ<color=orange>い</color>"}], ensure_ascii=False),
            encoding="utf-8",
        )
        self._write_state(1)

        sys.argv = ["verify_batch.py", "--stem", self.STEM, "--allow-warnings"]
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stdout(io.StringIO()):
                verify_batch.main()
        self.assertNotEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
