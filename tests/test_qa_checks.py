"""Deterministic QA rule and report-validation tests."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qa_checks import load_qa_policy, run_qa, validate_qa_report


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


if __name__ == "__main__":
    unittest.main()
