"""verify_batch 校验逻辑测试：actor 占位符豁免与人工放行。"""

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

    def _write_state(self, entries_count: int):
        state = {
            "current_batch": 0,
            "total_batches": 1,
            "batches": [[0, entries_count]],
            "export_file": str(self.export_file),
        }
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

    def test_actor_entry_exempt_from_tag_count(self):
        """占位符（actor）条目只保留占位符标签时不产生警告。"""
        entries = [
            {
                "id": "1",
                "source": (
                    "（う……）<tag id='1' type='br' desc='换行'/>"
                    "<tag id='2' type='fmt' desc='⟨actor⟩'/>"
                    "<tag id='3' type='br' desc='换行'/>クラウド"
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
                    {"id": "1", "target": "<tag id='2' type='fmt' desc='⟨actor⟩'/>"},
                    {"id": "2", "target": "あい"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._write_state(2)

        out = self._run()
        self.assertIn("1 条标签数与 source 不一致", out)
        self.assertNotIn("['1']", out)

    def test_allow_warnings_flag(self):
        """--allow-warnings 时输出 warnings accepted。"""
        entries = [
            {"id": "1", "source": "あ<tag id='1' type='br' desc='换行'/>い"},
        ]
        self.export_file.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
        )
        self.reviewed.write_text(
            json.dumps([{"id": "1", "target": "あい"}], ensure_ascii=False),
            encoding="utf-8",
        )
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
