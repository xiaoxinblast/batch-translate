"""Shared validation contract regression tests."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from validation import (
    effective_entry_policy,
    load_validation_policy,
    validate_batch_results,
)


class ValidationTest(unittest.TestCase):
    def test_rejects_extra_id_and_empty_target(self):
        expected = [{"id": "1", "source": "原文"}]
        report = validate_batch_results(
            [{"id": "1", "target": ""}, {"id": "2", "target": "越批"}],
            expected,
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("批外 id" in message for message in report.fatal))
        self.assertTrue(any("target 为空" in message for message in report.fatal))

    def test_locked_target_must_be_exact(self):
        expected = [{
            "id": "1", "source": "原文", "translated": "已交付", "locked": True,
        }]
        report = validate_batch_results(
            [{"id": "1", "target": "被修改"}], expected
        )
        self.assertTrue(any("preserved" in message for message in report.fatal))

    def test_preserved_target_skips_format_checks_but_cannot_change(self):
        source_tag = "<tag id='1' type='fmt' desc='源格式'/>"
        legacy_tag = "<tag id='9' type='fmt' desc='旧格式'/>"
        expected = [{
            "id": "1",
            "source": f"原{source_tag}文",
            "translated": f"旧{legacy_tag}译文",
            "locked": True,
            "preserve_existing": True,
        }]

        unchanged = validate_batch_results(
            [{"id": "1", "target": f"旧{legacy_tag}译文"}], expected
        )
        self.assertTrue(unchanged.ok, unchanged.fatal)

        changed = validate_batch_results(
            [{"id": "1", "target": f"改{legacy_tag}译文"}], expected
        )
        self.assertTrue(any("preserved" in message for message in changed.fatal))

    def test_source_locked_empty_target_is_allowed(self):
        expected = [{
            "id": "1", "source": "不可翻译", "target": "",
            "locked": True, "source_locked": True,
        }]
        report = validate_batch_results([{"id": "1", "target": ""}], expected)
        self.assertTrue(report.ok, report.fatal)

    def test_empty_target_requires_source_locked_or_explicit_policy(self):
        expected = [{
            "id": "1", "source": "原文", "target": "", "locked": True,
        }]
        report = validate_batch_results([{"id": "1", "target": ""}], expected)
        self.assertTrue(any("target 为空" in message for message in report.fatal))

    def test_default_policy_ignores_br_but_protects_other_tags(self):
        br = "<tag id='1' type='br' desc='换行'/>"
        fmt = "<tag id='2' type='fmt' desc='粗体开始'/>"
        expected = [{"id": "1", "source": f"原{br}{fmt}文"}]
        ok = validate_batch_results(
            [{"id": "1", "target": f"译{fmt}文"}], expected
        )
        self.assertTrue(ok.ok, ok.fatal)

        bad_fmt = "<tag id='9' type='fmt' desc='粗体开始'/>"
        bad = validate_batch_results(
            [{"id": "1", "target": f"译{bad_fmt}文"}], expected
        )
        self.assertTrue(any("标签序列" in message for message in bad.fatal))

    def test_project_policy_can_require_br_and_override_one_entry(self):
        br = "<tag id='1' type='br' desc='换行'/>"
        expected = [
            {"id": "1", "source": f"原{br}文"},
            {"id": "2", "source": f"原{br}文"},
        ]
        policy = load_validation_policy()
        policy["ignored_tag_types"] = []
        policy["entry_overrides"] = {"2": {"tag_mode": "ignore"}}
        report = validate_batch_results(
            [{"id": "1", "target": "译文"}, {"id": "2", "target": "译文"}],
            expected,
            policy,
        )
        self.assertTrue(any("id=1" in message for message in report.fatal))
        self.assertFalse(any("id=2" in message for message in report.fatal))

    def test_project_policy_can_retain_actor_while_forbidding_target_br(self):
        br = "<tag id='1' type='br' desc='换行'/>"
        actor = "<tag id='2' type='fmt' desc='⟨actor⟩'/>"
        expected = [{"id": "1", "source": f"原{br}{actor}{br}文"}]
        policy = load_validation_policy()
        policy["entry_overrides"] = {
            "1": {
                "tag_mode": "exact",
                "forbidden_target_tag_types": ["br"],
            }
        }

        valid = validate_batch_results(
            [{"id": "1", "target": f"译{actor}文"}], expected, policy
        )
        self.assertTrue(valid.ok, valid.fatal)

        missing_actor = validate_batch_results(
            [{"id": "1", "target": "译文"}], expected, policy
        )
        self.assertTrue(any("标签序列" in message for message in missing_actor.fatal))

        extra_br = validate_batch_results(
            [{"id": "1", "target": f"译{actor}{br}文"}], expected, policy
        )
        self.assertTrue(any("禁止标签类型" in message for message in extra_br.fatal))

    def test_effective_entry_policy_includes_global_empty_allowance(self):
        policy = load_validation_policy()
        policy["allow_empty_ids"] = ["2"]
        policy["entry_overrides"] = {
            "2": {"tag_mode": "ignore", "enforce_newline_count": True}
        }

        effective = effective_entry_policy(policy, "2")

        self.assertTrue(effective["allow_empty"])
        self.assertEqual(effective["tag_mode"], "ignore")
        self.assertTrue(effective["enforce_newline_count"])

    def test_maxlength_is_enforced(self):
        expected = [{"id": "1", "source": "原文", "maxlengthchars": "3"}]
        report = validate_batch_results(
            [{"id": "1", "target": "四个汉字"}], expected
        )
        self.assertTrue(any("maxlengthchars" in message for message in report.fatal))

    def test_policy_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "policy.json"
            path.write_text(json.dumps({"unknown": True}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_validation_policy(path)

    def test_policy_rejects_string_booleans(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(
                json.dumps({"enforce_newline_count": "false"}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_validation_policy(path)


if __name__ == "__main__":
    unittest.main()
