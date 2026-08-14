"""batch.py refresh 命令回归测试（重新 parse + 重跑增强）。"""

import json
import shutil
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


class RefreshTest(unittest.TestCase):
    STEM = "Refresh_Stem"

    @classmethod
    def setUpClass(cls):
        cls._orig_script_dir = batch._SCRIPT_DIR
        cls._orig_active = batch._ACTIVE_PROJECT
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        bt = base / "batch_translate"
        (bt / "data" / cls.STEM).mkdir(parents=True)
        (bt / "exports" / cls.STEM).mkdir(parents=True)
        # refresh 会调用 convert.py / mqxliff_tool.py，复制脚本到临时工具目录
        shutil.copytree(ROOT / "parsers", bt / "parsers")
        shutil.copy2(ROOT / "convert.py", bt / "convert.py")
        shutil.copy2(ROOT / "mqxliff_tool.py", bt / "mqxliff_tool.py")
        (bt / "data" / ".active_project").write_text(cls.STEM, encoding="utf-8")
        mq = bt / "data" / cls.STEM / f"_working_{cls.STEM}.mqxliff"
        mq.write_text(_MINI_MQ, encoding="utf-8")

        (bt / "data" / "tm_memory.json").write_text(json.dumps({"entries": [
            {"source": "こんにちは", "target": "你好呀", "context": "c1", "file": "f1"},
        ]}, ensure_ascii=False), encoding="utf-8")
        import openpyxl
        tb = bt / "data" / "term_base.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["原文(ja)", "译文(zh)", "注释"])
        ws.append(["こんにちは", "你好", "n"])
        wb.save(tb)
        wb.close()
        (bt / "data" / "style_guide.txt").write_text("规则", encoding="utf-8")

        batch._SCRIPT_DIR = bt
        batch._ACTIVE_PROJECT = bt / "data" / ".active_project"

    @classmethod
    def tearDownClass(cls):
        batch._SCRIPT_DIR = cls._orig_script_dir
        batch._ACTIVE_PROJECT = cls._orig_active
        cls._tmp.cleanup()

    def _working_path(self) -> Path:
        return batch._SCRIPT_DIR / "exports" / self.STEM / "_working.json"

    def _assert_refreshed(self):
        data = json.loads(self._working_path().read_text(encoding="utf-8"))
        self.assertEqual(len(data["entries"]), 1)
        e = data["entries"][0]
        self.assertIn("tm_matches", e)
        self.assertEqual(e["tm_matches"][0]["target"], "你好呀")
        self.assertIn("terms", e)
        self.assertEqual(e["terms"][0]["zh"], "你好")
        self.assertIn("style_guide", data)

    def test_refresh_without_state(self):
        batch.cmd_refresh(None, None, None, None)
        self._assert_refreshed()

    def test_refresh_with_state(self):
        bt = batch._SCRIPT_DIR
        state = {
            "stem": self.STEM,
            "source_file": str(bt / "data" / self.STEM / f"_working_{self.STEM}.mqxliff"),
            "tm_path": str(bt / "data" / "tm_memory.json"),
            "terms_path": str(bt / "data" / "term_base.xlsx"),
            "style_guide_path": str(bt / "data" / "style_guide.txt"),
        }
        (bt / "data" / self.STEM / "batch_state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.addCleanup((bt / "data" / self.STEM / "batch_state.json").unlink, missing_ok=True)
        batch.cmd_refresh(None, None, None, None)
        self._assert_refreshed()

    def test_refresh_with_state_can_override_permanent_tm(self):
        bt = batch._SCRIPT_DIR
        state = {
            "stem": self.STEM,
            "source_file": str(bt / "data" / self.STEM / f"_working_{self.STEM}.mqxliff"),
            "tm_path": str(bt / "data" / "tm_memory.json"),
            "terms_path": str(bt / "data" / "term_base.xlsx"),
            "style_guide_path": str(bt / "data" / "style_guide.txt"),
        }
        (bt / "data" / self.STEM / "batch_state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8")
        override = bt / "data" / "override_tm.json"
        override.write_text(json.dumps({"entries": [{
            "source": "こんにちは", "target": "覆盖译文", "context": "c1", "file": "override"
        }]}, ensure_ascii=False), encoding="utf-8")
        self.addCleanup((bt / "data" / self.STEM / "batch_state.json").unlink, missing_ok=True)
        self.addCleanup(override.unlink, missing_ok=True)

        batch.cmd_refresh(None, str(override), None, None)
        data = json.loads(self._working_path().read_text(encoding="utf-8"))
        self.assertEqual(data["entries"][0]["tm_matches"][0]["target"], "覆盖译文")

    def test_refresh_after_completion_uses_manifest_tm_layers(self):
        bt = batch._SCRIPT_DIR
        runtime_dir = bt / "data" / self.STEM / "tm_runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        runtime = runtime_dir / "_batch_001.json"
        runtime.write_text(json.dumps({"entries": [{
            "source": "こんにちは", "target": "运行期译文", "context": "", "file": "batch-1"
        }]}, ensure_ascii=False), encoding="utf-8")
        manifest = bt / "exports" / self.STEM / "project_manifest.json"
        manifest.write_text(json.dumps({
            "source_file": str(bt / "data" / self.STEM / f"_working_{self.STEM}.mqxliff"),
            "tm_permanent_path": str(bt / "data" / "tm_memory.json"),
            "tm_runtime_dir": str(runtime_dir),
            "tm_runtime_files": [str(runtime)],
        }, ensure_ascii=False), encoding="utf-8")
        self.addCleanup(runtime.unlink, missing_ok=True)
        self.addCleanup(manifest.unlink, missing_ok=True)

        batch.cmd_refresh(None, None, None, None)
        data = json.loads(self._working_path().read_text(encoding="utf-8"))
        self.assertEqual(data["entries"][0]["runtime_tm_matches"][0]["target"], "运行期译文")


if __name__ == "__main__":
    unittest.main()
