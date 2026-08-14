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

    def _disable_qa(self, stem: str) -> None:
        """Keep legacy transaction tests focused on write/rollback behavior."""
        state_path = self.tool / "data" / stem / "batch_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["qa_required"] = False
        state["qa_status"] = "not_started"
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _write_result(self, stem: str, batch_num: int, targets: dict[str, str]) -> Path:
        self._disable_qa(stem)
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
        entry_id = task["entries"][0]["id"]
        batch.cmd_submit(self._write_result("formatted", 1, {entry_id: translated}))

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

    def test_policy_allowed_empty_clears_unlocked_mqxliff_target(self):
        source = self.base / "clear.mqxliff"
        source.write_text(
            _MINI_MQ.replace(
                '<target xml:space="preserve"></target>',
                '<target xml:space="preserve">旧译文</target>',
                1,
            ),
            encoding="utf-8",
        )
        policy = self.base / "allow-empty.json"
        policy.write_text(
            json.dumps({"allow_empty_ids": ["1"]}), encoding="utf-8"
        )
        batch.cmd_init(source, validation_policy_path=policy)
        batch.cmd_next(review_only=True)
        self._disable_qa("clear")
        result = self.base / "clear-result.json"
        result.write_text(
            json.dumps([
                {"id": "1", "target": ""},
                {"id": "2", "target": "译文二"},
            ], ensure_ascii=False),
            encoding="utf-8",
        )

        batch.cmd_submit(result)
        delivered = self.base / "clear-delivered.mqxliff"
        batch.cmd_export("clear", str(delivered), False)

        from mqxliff_tool import parse_mqxliff

        units, _ = parse_mqxliff(delivered)
        self.assertEqual(next(unit for unit in units if unit.id == "1").target_text, "")
        self.assertEqual(next(unit for unit in units if unit.id == "2").target_text, "译文二")

    def test_translate_submit_requires_review_and_qa(self):
        source = self.base / "qa_gate.txt"
        source.write_text("原文\n", encoding="utf-8")
        batch.cmd_init(source)
        batch.cmd_next()
        result = self.base / "qa-gate-result.json"
        result.write_text(
            json.dumps([{"id": "1", "target": "译文"}], ensure_ascii=False),
            encoding="utf-8",
        )

        with self.assertRaises(SystemExit):
            batch.cmd_submit(result)
        state = self._state("qa_gate")
        self.assertEqual(state["qa_status"], "awaiting_translation")
        self.assertEqual(state["current_batch"], 0)

    def test_clean_qa_rejects_changed_reviewed_baseline(self):
        source = self.base / "qa_clean.txt"
        source.write_text("原文\n", encoding="utf-8")
        batch.cmd_init(source)
        batch.cmd_next()
        translation = self.base / "qa-clean-translation.json"
        translation.write_text(
            json.dumps([{"id": "1", "target": "译文"}], ensure_ascii=False),
            encoding="utf-8",
        )
        batch.cmd_review(translation)
        reviewed = self.tool / "exports" / "qa_clean" / "_batch_001_reviewed.json"
        reviewed.write_text(
            json.dumps([{"id": "1", "target": "译文"}], ensure_ascii=False),
            encoding="utf-8",
        )
        batch.cmd_qa("qa_clean")

        reviewed.write_text(
            json.dumps([{"id": "1", "target": "改后"}], ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            batch.cmd_submit(reviewed, "qa_clean")

    def test_qa_reviewer_fixes_machine_finding_before_commit(self):
        source = self.base / "qa.txt"
        source.write_text("こんにちは\n", encoding="utf-8")
        original = source.read_bytes()
        batch.cmd_init(source)
        batch.cmd_next()

        translation = self.base / "qa-translation.json"
        translation.write_text(
            json.dumps([{"id": "1", "target": "こんにちは"}], ensure_ascii=False),
            encoding="utf-8",
        )
        batch.cmd_review(translation)

        reviewed = self.tool / "exports" / "qa" / "_batch_001_reviewed.json"
        reviewed.write_text(
            json.dumps([{"id": "1", "target": "こんにちは"}], ensure_ascii=False),
            encoding="utf-8",
        )
        batch.cmd_qa("qa")
        qa_task = json.loads(
            (self.tool / "exports" / "qa" / "_batch_001_qa_task.json").read_text(
                encoding="utf-8"
            )
        )
        finding = qa_task["findings"][0]
        qa_result = Path(qa_task["qa_reviewed_path"])
        report = Path(qa_task["qa_report_path"])
        alternate_result = self.base / "qa-reviewed.json"
        alternate_report = self.base / "qa-report.json"
        alternate_result.write_text(
            json.dumps([{"id": "1", "target": "你好"}], ensure_ascii=False),
            encoding="utf-8",
        )
        alternate_report.write_text(
            json.dumps({"schema_version": 1, "findings": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            batch.cmd_qa_submit(alternate_result, alternate_report, "qa")
        qa_result.write_text(
            json.dumps([{"id": "1", "target": "你好"}], ensure_ascii=False),
            encoding="utf-8",
        )
        report.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "findings": [
                        {
                            "finding_id": finding["finding_id"],
                            "id": "1",
                            "status": "fixed",
                            "reason": "原文仍为日文，已改为中文译文",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        batch.cmd_qa_submit(qa_result, report, "qa")

        manifest = json.loads(
            (self.tool / "exports" / "qa" / "project_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["qa_history"][0]["status"], "agent_reviewed")
        self.assertEqual(source.read_bytes(), original)

    def test_out_of_batch_id_is_rejected_without_file_changes(self):
        source = self.base / "ids.txt"
        source.write_text("一\n二\n", encoding="utf-8")
        batch.cmd_init(source, batch_chars=1)
        batch.cmd_next()
        self._disable_qa("ids")
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

    def test_effective_validation_policy_is_injected_into_tasks(self):
        source = self.base / "policy.txt"
        source.write_text("原文\n", encoding="utf-8")
        policy = self.base / "validation_policy.json"
        policy.write_text(
            json.dumps({
                "ignored_tag_types": [],
                "allow_empty_ids": ["1"],
                "entry_overrides": {
                    "1": {
                        "tag_mode": "ignore",
                        "enforce_newline_count": True,
                    }
                },
            }),
            encoding="utf-8",
        )

        batch.cmd_init(source, validation_policy_path=policy)
        batch.cmd_next()
        task = json.loads(
            (self.tool / "exports" / "policy" / "_batch_001_to_translate.json")
            .read_text(encoding="utf-8")
        )

        validation = task["entries"][0]["validation"]
        self.assertEqual(validation["ignored_tag_types"], [])
        self.assertEqual(validation["tag_mode"], "ignore")
        self.assertTrue(validation["enforce_newline_count"])
        self.assertTrue(validation["allow_empty"])

    def test_submit_warnings_require_reason_and_are_recorded(self):
        source = self.base / "warnings.txt"
        source.write_text("原文\n", encoding="utf-8")
        batch.cmd_init(source)
        batch.cmd_next()
        self._disable_qa("warnings")
        wrapped = self.base / "wrapped.json"
        wrapped.write_text(
            json.dumps({"entries": [{"id": "1", "target": "译文"}]}, ensure_ascii=False),
            encoding="utf-8",
        )

        with self.assertRaises(SystemExit):
            batch.cmd_submit(wrapped)
        with self.assertRaises(SystemExit):
            batch.cmd_submit(wrapped, allow_warnings=True)

        batch.cmd_submit(
            wrapped,
            allow_warnings=True,
            warning_reason="项目接受对象包装",
        )

        manifest = json.loads(
            (self.tool / "exports" / "warnings" / "project_manifest.json")
            .read_text(encoding="utf-8")
        )
        acceptance = manifest["warning_acceptances"][0]
        self.assertEqual(acceptance["batch"], 1)
        self.assertEqual(acceptance["reason"], "项目接受对象包装")
        self.assertTrue(acceptance["warnings"])

    def test_same_stem_sources_use_distinct_project_ids(self):
        first_dir = self.base / "project_a"
        second_dir = self.base / "project_b"
        first_dir.mkdir()
        second_dir.mkdir()
        first = first_dir / "dialogue.txt"
        second = second_dir / "dialogue.txt"
        first.write_text("一\n", encoding="utf-8")
        second.write_text("二\n", encoding="utf-8")

        first_project = batch.cmd_init(first)
        second_project = batch.cmd_init(second)

        self.assertEqual(first_project, "dialogue")
        self.assertNotEqual(second_project, first_project)
        self.assertTrue(second_project.startswith("dialogue-"))
        first_state = self._state(first_project)
        second_state = self._state(second_project)
        self.assertEqual(Path(first_state["input_source_file"]), first.resolve())
        self.assertEqual(Path(second_state["input_source_file"]), second.resolve())
        self.assertEqual(
            batch._ACTIVE_PROJECT.read_text(encoding="utf-8"), second_project
        )

    def test_context_split_and_pack_cover_all_entries(self):
        source = self.base / "large.txt"
        source.write_text("一一\n二二\n三三\n", encoding="utf-8")
        batch.cmd_init(source)

        manifest_path = batch.cmd_context_split(3, "large")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["total_parts"], 3)
        part_entries = []
        report_paths = []
        for part in manifest["parts"]:
            payload = json.loads(Path(part["path"]).read_text(encoding="utf-8"))
            part_entries.extend(entry["id"] for entry in payload["entries"])
            report = self.base / f"part_{part['part']}.md"
            report.write_text(f"分片 {part['part']} 报告", encoding="utf-8")
            report_paths.append(report)
        self.assertEqual(part_entries, ["1", "2", "3"])

        merge_path = batch.cmd_context_pack(report_paths, None, "large")
        merge = json.loads(merge_path.read_text(encoding="utf-8"))

        self.assertEqual(merge["mode"], "context_merge")
        self.assertEqual(len(merge["part_reports"]), 3)

    def test_init_refuses_to_overwrite_existing_state(self):
        source = self.base / "existing.txt"
        source.write_text("一\n", encoding="utf-8")
        project_id = batch.cmd_init(source)
        state_path = self.tool / "data" / project_id / "batch_state.json"
        before = state_path.read_bytes()

        with self.assertRaises(SystemExit):
            batch.cmd_init(source)

        self.assertEqual(state_path.read_bytes(), before)

    def test_mqxliff_failure_rolls_back_work_json_tm_and_state(self):
        source = self.base / "rollback.mqxliff"
        source.write_text(_MINI_MQ, encoding="utf-8")
        tm = self.base / "tm.json"
        tm.write_text('{"entries": []}', encoding="utf-8")
        batch.cmd_init(source, batch_chars=1, tm_path=tm)
        batch.cmd_next()
        self._disable_qa("rollback")
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
        self._disable_qa("state")
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
