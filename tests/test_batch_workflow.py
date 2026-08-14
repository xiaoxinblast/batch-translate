"""批处理事务、独立导出和用户源文件只读的端到端测试。"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import openpyxl
from docx import Document

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import batch


_MINI_MQ = """<?xml version='1.0' encoding='UTF-8'?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" xmlns:mq="MQXliff" version="1.2">
<file original="t" source-language="ja" target-language="zh-cn" datatype="x-memoq">
<body>
<trans-unit id="1" mq:status="NotStarted" mq:locked="no">
<source xml:space="preserve">一</source><target xml:space="preserve"></target>
</trans-unit>
<trans-unit id="2" mq:status="NotStarted" mq:locked="no">
<source xml:space="preserve">二</source><target xml:space="preserve"></target>
</trans-unit>
</body>
</file>
</xliff>"""

_LOCKED_MQ = """<?xml version='1.0' encoding='UTF-8'?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" xmlns:mq="MQXliff" version="1.2">
<file original="t" source-language="ja" target-language="zh-cn" datatype="x-memoq">
<body>
<trans-unit id="1" mq:status="NotStarted" mq:locked="locked">
<source xml:space="preserve">锁定原文</source><target xml:space="preserve"></target>
</trans-unit>
</body>
</file>
</xliff>"""


class BatchWorkflowTest(unittest.TestCase):
    def setUp(self):
        self._orig_script_dir = batch._SCRIPT_DIR
        self._orig_active = batch._ACTIVE_PROJECT
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.tool = self.base / "batch_translate"
        self.tool.mkdir()
        (self.tool / "data").mkdir()
        (self.tool / "exports").mkdir()
        shutil.copy2(ROOT / "convert.py", self.tool / "convert.py")
        shutil.copy2(ROOT / "mqxliff_tool.py", self.tool / "mqxliff_tool.py")
        shutil.copy2(ROOT / "tm_store.py", self.tool / "tm_store.py")
        shutil.copytree(ROOT / "parsers", self.tool / "parsers")
        batch._SCRIPT_DIR = self.tool
        batch._ACTIVE_PROJECT = self.tool / "data" / ".active_project"

    def tearDown(self):
        batch._SCRIPT_DIR = self._orig_script_dir
        batch._ACTIVE_PROJECT = self._orig_active
        self._tmp.cleanup()

    def _state(self, stem: str) -> dict:
        return json.loads(
            (self.tool / "data" / stem / "batch_state.json").read_text(
                encoding="utf-8"
            )
        )

    def _write_result(self, stem: str, batch_num: int, targets: dict[str, str]) -> Path:
        task = json.loads(
            (self.tool / "exports" / stem / f"_batch_{batch_num:03d}_to_translate.json")
            .read_text(encoding="utf-8")
        )
        result = [
            {"id": entry["id"], "target": targets[entry["id"]]}
            for entry in task["entries"]
        ]
        path = self.base / f"result_{stem}_{batch_num}.json"
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return path

    def test_txt_source_is_unchanged_and_export_is_separate(self):
        source = self.base / "dialogue.txt"
        source.write_bytes("一\n\n二\n".encode("utf-8"))
        original = source.read_bytes()

        batch.cmd_init(source, batch_chars=1)
        batch.cmd_next()
        batch.cmd_submit(self._write_result("dialogue", 1, {"1": "甲"}))
        batch.cmd_submit(self._write_result("dialogue", 2, {"3": "乙"}))

        delivered = self.base / "delivered.txt"
        batch.cmd_export("dialogue", str(delivered), False)
        self.assertEqual(source.read_bytes(), original)
        self.assertNotEqual(delivered.resolve(), source.resolve())
        self.assertEqual(delivered.read_text(encoding="utf-8"), "甲\n\n乙\n")

        with self.assertRaises(SystemExit):
            batch.cmd_export("dialogue", str(source), True)
        self.assertEqual(source.read_bytes(), original)

    def test_xlsx_custom_columns_survive_multiple_batches(self):
        source = self.base / "sheet.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Strings"
        ws.append(["meta", "unused", "source", "target"])
        ws.append(["meta", "unused", "header 2", ""])
        ws.append(["meta", "unused", "header 3", ""])
        ws.append(["m1", "NOISE1", "原文一", "既有译文"])
        ws.append(["m2", "NOISE2", "原文二", ""])
        ws.append(["m3", "NOISE3", "原文三", ""])
        wb.save(source)
        wb.close()
        original = source.read_bytes()

        batch.cmd_init(
            source,
            batch_chars=3,
            source_col="C",
            target_col="D",
            header_row=3,
            sheet_name="Strings",
        )
        batch.cmd_next()
        first_task = json.loads(
            (self.tool / "exports" / "sheet" / "_batch_001_to_translate.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(first_task["entries"][0]["source"], "原文一")
        batch.cmd_submit(
            self._write_result("sheet", 1, {"1": "既有译文"})
        )
        second_task = json.loads(
            (self.tool / "exports" / "sheet" / "_batch_002_to_translate.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(second_task["entries"][0]["source"], "原文二")
        batch.cmd_submit(self._write_result("sheet", 2, {"2": "译文二"}))
        third_task = json.loads(
            (self.tool / "exports" / "sheet" / "_batch_003_to_translate.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(third_task["entries"][0]["source"], "原文三")
        batch.cmd_submit(self._write_result("sheet", 3, {"3": "译文三"}))

        delivered = self.base / "sheet_translated.xlsx"
        batch.cmd_export("sheet", str(delivered), False)
        self.assertEqual(source.read_bytes(), original)
        result_wb = openpyxl.load_workbook(delivered, data_only=True)
        result_ws = result_wb["Strings"]
        self.assertEqual(result_ws["D4"].value, "既有译文")
        self.assertEqual(result_ws["D5"].value, "译文二")
        self.assertEqual(result_ws["D6"].value, "译文三")
        result_wb.close()

    def test_docx_source_is_unchanged_and_formatting_is_exported(self):
        source = self.base / "formatted.docx"
        doc = Document()
        paragraph = doc.add_paragraph()
        paragraph.add_run("原文").bold = True
        doc.save(source)
        original = source.read_bytes()

        batch.cmd_init(source)
        batch.cmd_next()
        task = json.loads(
            (self.tool / "exports" / "formatted" / "_batch_001_to_translate.json")
            .read_text(encoding="utf-8")
        )
        translated = task["entries"][0]["source"].replace("原文", "译文")
        batch.cmd_submit(self._write_result("formatted", 1, {"1": translated}))

        delivered = self.base / "formatted_translated.docx"
        batch.cmd_export("formatted", str(delivered), False)
        self.assertEqual(source.read_bytes(), original)
        result_doc = Document(delivered)
        self.assertEqual(result_doc.paragraphs[0].text, "译文")
        self.assertTrue(result_doc.paragraphs[0].runs[0].bold)

    def test_source_locked_empty_mqxliff_completes_without_touching_source(self):
        source = self.base / "locked.mqxliff"
        source.write_text(_LOCKED_MQ, encoding="utf-8")
        original = source.read_bytes()

        batch.cmd_init(source)
        batch.cmd_next()
        task_path = self.tool / "exports" / "locked" / "_batch_001_to_translate.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        self.assertTrue(task["entries"][0]["source_locked"])
        self.assertTrue(task["entries"][0]["locked"])
        self.assertEqual(task["entries"][0]["target"], "")
        batch.cmd_submit(self._write_result("locked", 1, {"1": ""}))

        delivered = self.base / "locked_translated.mqxliff"
        batch.cmd_export("locked", str(delivered), False)
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue(delivered.is_file())

    def test_out_of_batch_id_is_rejected_without_file_changes(self):
        source = self.base / "ids.txt"
        source.write_text("一\n二\n", encoding="utf-8")
        batch.cmd_init(source, batch_chars=1)
        batch.cmd_next()
        state = self._state("ids")
        tracked = [
            source,
            Path(state["source_file"]),
            Path(state["export_file"]),
            self.tool / "data" / "ids" / "batch_state.json",
        ]
        before = {path: path.read_bytes() for path in tracked}
        bad = self.base / "bad.json"
        bad.write_text(
            json.dumps([{"id": "1", "target": "甲"}, {"id": "2", "target": "乙"}], ensure_ascii=False),
            encoding="utf-8",
        )

        with self.assertRaises(SystemExit):
            batch.cmd_submit(bad)
        self.assertEqual({path: path.read_bytes() for path in tracked}, before)

    def test_invalid_validation_policy_fails_before_copying_source(self):
        source = self.base / "policy.txt"
        source.write_text("原文\n", encoding="utf-8")
        policy = self.base / "bad_policy.json"
        policy.write_text('{"unknown": true}', encoding="utf-8")

        with self.assertRaises(SystemExit):
            batch.cmd_init(source, validation_policy_path=policy)
        self.assertFalse((self.tool / "data" / "policy").exists())
        self.assertFalse(batch._ACTIVE_PROJECT.exists())

    def test_mqxliff_failure_rolls_back_work_json_tm_and_state(self):
        source = self.base / "rollback.mqxliff"
        source.write_text(_MINI_MQ, encoding="utf-8")
        tm = self.base / "tm.json"
        tm.write_text('{"entries": []}', encoding="utf-8")
        batch.cmd_init(source, batch_chars=1, tm_path=tm)
        batch.cmd_next()
        state = self._state("rollback")
        tracked = [
            source,
            Path(state["source_file"]),
            Path(state["export_file"]),
            tm,
            self.tool / "data" / "rollback" / "batch_state.json",
        ]
        before = {path: path.read_bytes() for path in tracked}
        result = self._write_result("rollback", 1, {"1": "甲"})
        real_run = __import__("subprocess").run

        def fail_reparse(args, *pargs, **kwargs):
            if "convert.py" in str(args[1]) and "parse" in args:
                raise __import__("subprocess").CalledProcessError(1, args)
            return real_run(args, *pargs, **kwargs)

        with mock.patch("subprocess.run", side_effect=fail_reparse):
            with self.assertRaises(__import__("subprocess").CalledProcessError):
                batch.cmd_submit(result)
        self.assertEqual({path: path.read_bytes() for path in tracked}, before)

    def test_state_write_failure_rolls_back_everything(self):
        source = self.base / "state.txt"
        source.write_text("一\n二\n", encoding="utf-8")
        batch.cmd_init(source, batch_chars=1)
        batch.cmd_next()
        state = self._state("state")
        state_path = self.tool / "data" / "state" / "batch_state.json"
        tracked = [source, Path(state["source_file"]), Path(state["export_file"]), state_path]
        before = {path: path.read_bytes() for path in tracked}
        result = self._write_result("state", 1, {"1": "甲"})

        def corrupt_then_fail(updated_state):
            state_path.write_text('{"corrupt": true}', encoding="utf-8")
            raise OSError("injected state write failure")

        with mock.patch.object(batch, "_save_state", side_effect=corrupt_then_fail):
            with self.assertRaises(OSError):
                batch.cmd_submit(result)
        self.assertEqual({path: path.read_bytes() for path in tracked}, before)


if __name__ == "__main__":
    unittest.main()
