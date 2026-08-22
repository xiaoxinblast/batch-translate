"""Deterministic QA rule and report-validation tests."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qa_checks import load_qa_policy, run_qa, run_qa_plugin, validate_qa_report


class QaChecksTest(unittest.TestCase):
    def test_exact_tm_target_mismatch_is_reported(self):
        expected = [
            {
                "id": "1",
                "source": "こんにちは",
                "tm_matches": [{"similarity": 1.0, "target": "你好"}],
            }
        ]

        result = run_qa(
            [{"id": "1", "target": "您好"}],
            expected,
        )

        self.assertEqual(
            [finding["rule"] for finding in result["findings"]],
            ["tm_exact_target_mismatch"],
        )
        self.assertEqual(result["findings"][0]["tm_targets"], ["你好"])

    def test_exact_tm_target_match_is_clean(self):
        expected = [
            {
                "id": "1",
                "source": "こんにちは",
                "tm_matches": [{"similarity": 1, "target": "你好"}],
            }
        ]

        result = run_qa(
            [{"id": "1", "target": "你好"}],
            expected,
        )

        self.assertEqual(result["findings"], [])

    def test_permanent_exact_tm_wins_over_runtime_exact_tm(self):
        expected = [{
            "id": "1",
            "source": "こんにちは",
            "tm_matches": [{"similarity": 1.0, "target": "永久译文"}],
            "runtime_tm_matches": [{"similarity": 1.0, "target": "运行期译文"}],
        }]

        permanent_result = run_qa(
            [{"id": "1", "target": "永久译文"}],
            expected,
        )
        self.assertEqual(permanent_result["findings"], [])

        runtime_result = run_qa(
            [{"id": "1", "target": "运行期译文"}],
            expected,
        )
        self.assertEqual(runtime_result["findings"][0]["tm_scope"], "permanent")

    def test_runtime_exact_tm_is_used_without_permanent_exact_match(self):
        expected = [{
            "id": "1",
            "source": "こんにちは",
            "runtime_tm_matches": [{"similarity": 1.0, "target": "你好"}],
        }]
        result = run_qa(
            [{"id": "1", "target": "您好"}],
            expected,
        )
        self.assertEqual(result["findings"][0]["tm_scope"], "runtime")

    def test_newline_check_is_opt_in(self):
        expected = [{"id": "1", "source": "第一行\n第二行"}]
        results = [{"id": "1", "target": "第一行 第二行"}]

        default_result = run_qa(results, expected)
        self.assertNotIn(
            "newline_count", {finding["rule"] for finding in default_result["findings"]}
        )

        policy = load_qa_policy()
        policy["rules"]["newline_count"]["enabled"] = True
        strict_result = run_qa(results, expected, policy)
        self.assertIn(
            "newline_count", {finding["rule"] for finding in strict_result["findings"]}
        )

    def test_newline_semantics_blocks_trailing_and_triple_breaks(self):
        br1 = "<tag id='1' type='br' desc='换行'/>"
        br2 = "<tag id='2' type='br' desc='换行'/>"
        expected = [{"id": "1", "source": f"定义{br1}{br2}后续动作"}]
        target = f"定义{br1}{br2}{br1}后续动作"

        result = run_qa([{"id": "1", "target": target}], expected)

        findings = [item for item in result["findings"] if item["rule"] == "newline_semantics"]
        self.assertTrue(findings)
        self.assertEqual(findings[0]["severity"], "error")
        self.assertIn("source", findings[0]["layout"])

    def test_suspicious_newline_break_is_a_warning(self):
        br = "<tag id='1' type='br' desc='换行'/>"
        result = run_qa(
            [{"id": "1", "target": f"Version{br}42"}],
            [{"id": "1", "source": f"Version{br}42"}],
        )
        findings = [item for item in result["findings"] if item["rule"] == "newline_suspicious_break"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warning")

    def test_number_parity_allows_equivalent_chinese_numerals(self):
        expected = [{"id": "1", "source": "第３章"}]

        result = run_qa([{"id": "1", "target": "第三章"}], expected)

        self.assertNotIn(
            "number_parity", {finding["rule"] for finding in result["findings"]}
        )

    def test_number_parity_rejects_different_chinese_numeral(self):
        expected = [{"id": "1", "source": "第１２３章"}]

        result = run_qa([{"id": "1", "target": "第一百二十四章"}], expected)

        self.assertIn(
            "number_parity", {finding["rule"] for finding in result["findings"]}
        )

    def test_number_parity_allows_chinese_percentage(self):
        expected = [{"id": "1", "source": "成功率１２％"}]

        result = run_qa([{"id": "1", "target": "成功率百分之十二"}], expected)

        self.assertNotIn(
            "number_parity", {finding["rule"] for finding in result["findings"]}
        )

    def test_number_parity_ignores_chinese_words_when_source_has_no_digits(self):
        expected = [{"id": "1", "source": "一度だけ許してしまえば"}]

        result = run_qa([{"id": "1", "target": "一旦放任下去"}], expected)

        self.assertNotIn(
            "number_parity", {finding["rule"] for finding in result["findings"]}
        )

    def test_entry_override_disables_duplicate_consistency(self):
        expected = [{
            "id": "1",
            "source": "same source",
            "target": "译文一",
        }]
        all_entries = [
            {"id": "1", "source": "same source", "target": "译文一"},
            {"id": "2", "source": "same source", "target": "译文二"},
        ]

        default_result = run_qa(
            [{"id": "1", "target": "译文一"}], expected, all_entries=all_entries
        )
        self.assertIn(
            "duplicate_consistency",
            {finding["rule"] for finding in default_result["findings"]},
        )

        policy = load_qa_policy()
        policy["entry_overrides"] = {"1": {"disabled_rules": ["duplicate_consistency"]}}
        overridden_result = run_qa(
            [{"id": "1", "target": "译文一"}],
            expected,
            policy,
            all_entries=all_entries,
        )
        self.assertNotIn(
            "duplicate_consistency",
            {finding["rule"] for finding in overridden_result["findings"]},
        )

    def test_locked_entry_is_not_checked(self):
        expected = [{
            "id": "1",
            "source": "こんにちは",
            "target": "こんにちは",
            "locked": True,
        }]

        result = run_qa(
            [{"id": "1", "target": "改动后的译文"}],
            expected,
        )

        self.assertEqual(result["findings"], [])

    def test_report_requires_every_finding_and_valid_status(self):
        machine_findings = [{
            "finding_id": "untranslated:1:abc",
            "id": "1",
            "rule": "untranslated",
        }]
        baseline = [{"id": "1", "target": "こんにちは"}]
        corrected = [{"id": "1", "target": "你好"}]
        report = {
            "schema_version": 1,
            "findings": [{
                "finding_id": "untranslated:1:abc",
                "id": "1",
                "status": "fixed",
                "reason": "译文已改为中文",
            }],
        }

        self.assertEqual(
            validate_qa_report(report, machine_findings, baseline, corrected), []
        )

        missing = {"schema_version": 1, "findings": []}
        errors = validate_qa_report(missing, machine_findings, baseline, corrected)
        self.assertTrue(any("未逐条处理" in error for error in errors))

    def test_report_rejects_finding_entry_id_mismatch(self):
        machine_findings = [{
            "finding_id": "untranslated:1:abc",
            "id": "1",
            "rule": "untranslated",
        }]
        report = {
            "schema_version": 1,
            "findings": [{
                "finding_id": "untranslated:1:abc",
                "id": "2",
                "status": "false_positive",
                "reason": "错误条目",
            }],
        }

        errors = validate_qa_report(
            report,
            machine_findings,
            [{"id": "1", "target": "原译文"}],
            [{"id": "1", "target": "原译文"}],
        )
        self.assertTrue(any("id 不匹配" in error for error in errors))

    def test_report_rejects_structural_newline_false_positive(self):
        finding = {
            "finding_id": "newline_semantics:1:abc",
            "id": "1",
            "rule": "newline_semantics",
        }
        report = {
            "schema_version": 1,
            "findings": [{
                "finding_id": finding["finding_id"],
                "id": "1",
                "status": "false_positive",
                "reason": "不应绕过结构性错误",
            }],
        }
        errors = validate_qa_report(
            report, [finding], [{"id": "1", "target": "原译文"}],
            [{"id": "1", "target": "原译文"}],
        )
        self.assertTrue(any("结构性换行" in error for error in errors))

    def test_plugin_can_only_append_valid_findings(self):
        with tempfile.TemporaryDirectory() as name:
            plugin = Path(name) / "qa_plugin.py"
            plugin.write_text(
                "import json, sys\n"
                "payload = json.load(sys.stdin)\n"
                "json.dump({'findings': [{'id': '1', 'rule': 'project_note', 'severity': 'warning', 'message': '需要人工确认'}]}, sys.stdout)\n",
                encoding="utf-8",
            )
            findings = run_qa_plugin(
                {"path": str(plugin), "timeout_seconds": 1},
                [{"id": "1", "target": "译文"}], [{"id": "1", "source": "原文"}], [],
            )
        self.assertEqual(findings[0]["rule"], "plugin:project_note")
        self.assertTrue(findings[0]["plugin"])

    def test_plugin_rejects_extra_output_fields_and_timeout(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            malformed = directory / "malformed.py"
            malformed.write_text(
                "import json\nprint(json.dumps({'findings': [], 'suppress_default': True}))\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "只能包含 findings"):
                run_qa_plugin(
                    {"path": str(malformed), "timeout_seconds": 1}, [], [], [],
                )
            slow = directory / "slow.py"
            slow.write_text("import time\ntime.sleep(1)\nprint('{\"findings\": []}')\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "超时"):
                run_qa_plugin(
                    {"path": str(slow), "timeout_seconds": 0.01}, [], [], [],
                )


if __name__ == "__main__":
    unittest.main()
