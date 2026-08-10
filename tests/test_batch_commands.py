"""batch.py 新增命令（summary/export/term-gaps）的回归测试。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import batch

_MINI_MQ = """<?xml version='1.0' encoding='UTF-8'?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" xmlns:mq="MQXliff" version="1.2">
<file original="t" source-language="ja" target-language="zh-cn" datatype="x-memoq">
<body>
<trans-unit id="1" mq:status="Pretranslated" mq:locked="no">
<source xml:space="preserve">こんにちは</source>
<target xml:space="preserve">你好</target>
</trans-unit>
</body>
</file>
</xliff>"""


class BatchCommandsTest(unittest.TestCase):
    STEM = "Test_Stem"

    @classmethod
    def setUpClass(cls):
        cls._orig_script_dir = batch._SCRIPT_DIR
        cls._orig_active = batch._ACTIVE_PROJECT
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        bt = base / "batch_translate"
        (bt / "data").mkdir(parents=True)
        (bt / "exports").mkdir()
        (base / "已交付").mkdir()
        (base / "_temp").mkdir()
        (bt / "data" / cls.STEM).mkdir()
        (bt / "exports" / cls.STEM).mkdir()
        (bt / "data" / ".active_project").write_text(cls.STEM, encoding="utf-8")
        (bt / "data" / cls.STEM / "batch_state.json").write_text(
            json.dumps({"document_summary": "old", "total": 1}, ensure_ascii=False),
            encoding="utf-8",
        )
        mq = bt / "data" / cls.STEM / f"_working_{cls.STEM}.mqxliff"
        mq.write_text(_MINI_MQ, encoding="utf-8")
        batch._SCRIPT_DIR = bt
        batch._ACTIVE_PROJECT = bt / "data" / ".active_project"

    @classmethod
    def tearDownClass(cls):
        batch._SCRIPT_DIR = cls._orig_script_dir
        batch._ACTIVE_PROJECT = cls._orig_active
        cls._tmp.cleanup()

    def _report(self, text: str) -> Path:
        p = Path(self._tmp.name) / "report.md"
        p.write_text(text, encoding="utf-8")
        return p

    def _state(self) -> dict:
        return json.loads(
            (batch._SCRIPT_DIR / "data" / self.STEM / "batch_state.json").read_text(
                encoding="utf-8"
            )
        )

    def _ensure_state(self):
        sp = batch._SCRIPT_DIR / "data" / self.STEM / "batch_state.json"
        if not sp.exists():
            sp.write_text(
                json.dumps({"document_summary": "old", "total": 1}, ensure_ascii=False),
                encoding="utf-8",
            )
        return sp

    def test_summary_writes_state_and_sidecar(self):
        self._ensure_state()
        report = self._report("==== 语境分析报告 ====\n正文内容")
        batch.cmd_summary(report, None)
        self.assertEqual(self._state()["document_summary"], "==== 语境分析报告 ====\n正文内容")
        sidecar = batch._SCRIPT_DIR / "exports" / self.STEM / "document_summary.md"
        self.assertTrue(sidecar.is_file())
        self.assertEqual(sidecar.read_text(encoding="utf-8"), "==== 语境分析报告 ====\n正文内容")

    def test_summary_sidecar_only_when_state_missing(self):
        state_path = batch._SCRIPT_DIR / "data" / self.STEM / "batch_state.json"
        state_path.unlink()
        self.addCleanup(self._ensure_state)
        report = self._report("仅 sidecar")
        batch.cmd_summary(report, None)  # 不应抛异常
        sidecar = batch._SCRIPT_DIR / "exports" / self.STEM / "document_summary.md"
        self.assertEqual(sidecar.read_text(encoding="utf-8"), "仅 sidecar")

    def test_export_copies_to_delivered_default(self):
        dst = Path(self._tmp.name) / "已交付" / f"{self.STEM}.mqxliff"
        if dst.exists():
            dst.unlink()
        batch.cmd_export(None, None, False)
        self.assertTrue(dst.is_file())
        self.assertEqual(dst.read_text(encoding="utf-8"), _MINI_MQ)

    def test_export_refuses_overwrite_without_force(self):
        dst = Path(self._tmp.name) / "已交付" / f"{self.STEM}.mqxliff"
        dst.write_text("OLD", encoding="utf-8")
        with self.assertRaises(SystemExit):
            batch.cmd_export(None, None, False)
        self.assertEqual(dst.read_text(encoding="utf-8"), "OLD")
        batch.cmd_export(None, None, True)
        self.assertEqual(dst.read_text(encoding="utf-8"), _MINI_MQ)

    def test_export_custom_out(self):
        out = Path(self._tmp.name) / "_temp" / "custom.mqxliff"
        batch.cmd_export(None, str(out), False)
        self.assertTrue(out.is_file())

    def test_term_gaps_from_sidecar(self):
        sidecar = batch._SCRIPT_DIR / "exports" / self.STEM / "document_summary.md"
        sidecar.write_text(
            "前言\n==== 五、疑似术语库未覆盖的专名 ====\n"
            "ジュディ | 朱迪（TM 先例） | 234 次\n",
            encoding="utf-8",
        )
        out = Path(self._tmp.name) / "_temp" / "gaps.md"
        batch.cmd_term_gaps(None, str(out))
        self.assertIn("朱迪", out.read_text(encoding="utf-8"))

    def test_term_gaps_fallback_to_batch_json(self):
        sidecar = batch._SCRIPT_DIR / "exports" / self.STEM / "document_summary.md"
        if sidecar.exists():
            sidecar.unlink()
        batch_file = batch._SCRIPT_DIR / "exports" / self.STEM / "_batch_001_to_translate.json"
        batch_file.write_text(
            json.dumps({
                "document_summary": "==== 疑似术语库未覆盖的专名 ====\nニック | 尼克 | 140 次",
                "entries": [],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        out = Path(self._tmp.name) / "_temp" / "gaps2.md"
        batch.cmd_term_gaps(None, str(out))
        self.assertIn("尼克", out.read_text(encoding="utf-8"))

    def test_term_gaps_missing_sources(self):
        sidecar = batch._SCRIPT_DIR / "exports" / self.STEM / "document_summary.md"
        batch_file = batch._SCRIPT_DIR / "exports" / self.STEM / "_batch_001_to_translate.json"
        if sidecar.exists():
            sidecar.unlink()
        if batch_file.exists():
            batch_file.unlink()
        with self.assertRaises(SystemExit):
            batch.cmd_term_gaps(None, None)


if __name__ == "__main__":
    unittest.main()
