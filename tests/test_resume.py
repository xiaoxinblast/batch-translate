"""init --resume 行为测试。"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import batch


def _mqxliff_with_target() -> str:
    return """<?xml version='1.0' encoding='UTF-8'?>
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


class InitResumeTest(unittest.TestCase):
    STEM = "resume"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.bt = self.base / "batch_translate"
        (self.bt / "data").mkdir(parents=True)
        (self.bt / "exports").mkdir()
        (self.bt / "data" / self.STEM).mkdir()
        (self.bt / "exports" / self.STEM).mkdir()
        # init 通过 subprocess 调用 convert.py，需把运行时文件复制到临时工具目录
        for name in ("convert.py", "mqxliff_tool.py", "term_base.py", "tm_store.py"):
            shutil.copy2(ROOT / name, self.bt / name)
        shutil.copytree(ROOT / "parsers", self.bt / "parsers")
        self.src = self.base / "resume.mqxliff"
        self.src.write_text(_mqxliff_with_target(), encoding="utf-8")
        self._orig_script_dir = batch._SCRIPT_DIR
        self._orig_active = batch._ACTIVE_PROJECT
        batch._SCRIPT_DIR = self.bt
        batch._ACTIVE_PROJECT = self.bt / "data" / ".active_project"

    def tearDown(self):
        batch._SCRIPT_DIR = self._orig_script_dir
        batch._ACTIVE_PROJECT = self._orig_active
        self.tmp.cleanup()

    def test_resume_without_state_detects_existing_targets(self):
        batch.cmd_init(self.src, resume=True)
        state = json.loads(
            (self.bt / "data" / self.STEM / "batch_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["total"], 1)
        self.assertEqual(state["existing_targets"], 1)
        work = self.bt / "data" / self.STEM / f"_working_{self.STEM}.mqxliff"
        self.assertTrue(work.is_file())

    def test_resume_with_existing_state_does_not_overwrite(self):
        state_path = self.bt / "data" / self.STEM / "batch_state.json"
        state_path.write_text(json.dumps({"marker": "keep-me"}, ensure_ascii=False), encoding="utf-8")
        batch.cmd_init(self.src, resume=True)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state, {"marker": "keep-me"})


if __name__ == "__main__":
    unittest.main()
