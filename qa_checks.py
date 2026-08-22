#!/usr/bin/env python3
"""Deterministic, project-configurable QA checks for translated batches."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from layout import layout_preview, suspicious_newline_warnings, validate_newline_semantics


DEFAULT_QA_POLICY: dict[str, Any] = {
    "rules": {
        "tm_exact_target_mismatch": {"enabled": True, "severity": "error"},
        "protected_token_parity": {"enabled": True, "severity": "error"},
        "number_parity": {
            "enabled": True, "severity": "warning", "comparison": "numeric_value",
        },
        "url_email_parity": {"enabled": True, "severity": "error"},
        "whitespace": {"enabled": True, "severity": "warning"},
        "punctuation_balance": {"enabled": True, "severity": "warning"},
        "untranslated": {"enabled": True, "severity": "warning"},
        "length_ratio": {"enabled": True, "severity": "warning"},
        "term_consistency": {"enabled": True, "severity": "warning"},
        "duplicate_consistency": {"enabled": True, "severity": "warning"},
        "newline_count": {"enabled": False, "severity": "warning"},
        "newline_semantics": {"enabled": True, "severity": "error"},
        "newline_suspicious_break": {"enabled": True, "severity": "warning"},
    },
    "protected_patterns": [
        r"\$\{[^{}]+\}",
        r"%(?:\d+\$)?[sdif]",
        r"\{\d+\}",
        r"\\[nrt]",
    ],
    "length_ratio": {"min": 0.25, "max": 3.0, "min_source_chars": 4},
    "ignore_rule_ids": [],
    "entry_overrides": {},
    "plugin": None,
}

_RULE_IDS = set(DEFAULT_QA_POLICY["rules"])
_RULE_KEYS = {"enabled", "severity", "comparison"}
_SEVERITIES = {"error", "warning", "info"}
_TOP_LEVEL_KEYS = set(DEFAULT_QA_POLICY)
_ENTRY_OVERRIDE_KEYS = {"disabled_rules", "rules"}
_TAG_RE = re.compile(r"<tag\b[^<>]*/>")
_BARE_TAG_RE = re.compile(r"<\/?[a-zA-Z][^>]*>")
_NUMBER_RE = re.compile(r"(?<!\d)\d+(?:[.,]\d+)?%?")
_CHINESE_NUMBER_RE = re.compile(r"[零〇一二三四五六七八九十百千万亿兆两]+%?")
_CHINESE_PERCENT_RE = re.compile(r"百分之[零〇一二三四五六七八九十百千万亿兆两]+")
_URL_EMAIL_RE = re.compile(
    r"(?:https?://|ftp://|www\.)[^\s<>\"']+"
    r"|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"
)
_JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_BRACKET_PAIRS = {
    "(": ")", "[": "]", "{": "}",
    "（": "）", "［": "］", "｛": "｝",
    "【": "】", "「": "」", "『": "』", "〈": "〉", "《": "》",
}
_BRACKET_CLOSES = set(_BRACKET_PAIRS.values())


def _clone_default() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_QA_POLICY, ensure_ascii=False))


def load_qa_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate an optional QA policy over safe generic defaults."""
    policy = _clone_default()
    if path:
        policy_path = Path(path)
        if not policy_path.is_file():
            raise FileNotFoundError(f"QA 策略不存在: {policy_path}")
        with open(policy_path, "r", encoding="utf-8") as f:
            custom = json.load(f)
        if not isinstance(custom, dict):
            raise ValueError("QA 策略必须是 JSON 对象")
        unknown = set(custom) - _TOP_LEVEL_KEYS
        if unknown:
            raise ValueError(f"QA 策略含未知字段: {sorted(unknown)}")
        for key, value in custom.items():
            if key == "rules":
                if not isinstance(value, dict):
                    raise ValueError("QA 策略 rules 必须是对象")
                policy["rules"].update(value)
            elif key == "length_ratio":
                if not isinstance(value, dict):
                    raise ValueError("QA 策略 length_ratio 必须是对象")
                policy["length_ratio"].update(value)
            elif key == "entry_overrides":
                if not isinstance(value, dict):
                    raise ValueError("QA 策略 entry_overrides 必须是对象")
                policy["entry_overrides"].update(value)
            else:
                policy[key] = value
    _validate_qa_policy(policy)
    return policy


def _validate_qa_policy(policy: dict[str, Any]) -> None:
    unknown = set(policy) - _TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"QA 策略含未知字段: {sorted(unknown)}")
    rules = policy.get("rules")
    if not isinstance(rules, dict):
        raise ValueError("QA 策略 rules 必须是对象")
    unknown_rules = set(rules) - _RULE_IDS
    if unknown_rules:
        raise ValueError(f"QA 策略含未知规则: {sorted(unknown_rules)}")
    for rule_id, config in rules.items():
        if not isinstance(config, dict):
            raise ValueError(f"QA 规则 {rule_id} 必须是对象")
        unknown_keys = set(config) - _RULE_KEYS
        if unknown_keys:
            raise ValueError(f"QA 规则 {rule_id} 含未知字段: {sorted(unknown_keys)}")
        if "enabled" in config and not isinstance(config["enabled"], bool):
            raise ValueError(f"QA 规则 {rule_id}.enabled 必须是 boolean")
        if config.get("severity", "warning") not in _SEVERITIES:
            raise ValueError(f"QA 规则 {rule_id}.severity 无效")
        if "comparison" in config:
            if rule_id != "number_parity" or config["comparison"] not in {
                "literal", "numeric_value"
            }:
                raise ValueError(f"QA 规则 {rule_id}.comparison 无效")

    patterns = policy.get("protected_patterns")
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise ValueError("QA 策略 protected_patterns 必须是字符串数组")
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"QA protected_patterns 含无效正则: {pattern!r}: {exc}") from exc

    ratio = policy.get("length_ratio")
    if not isinstance(ratio, dict) or set(ratio) - {"min", "max", "min_source_chars"}:
        raise ValueError("QA 策略 length_ratio 字段无效")
    for key in ("min", "max", "min_source_chars"):
        if not isinstance(ratio.get(key), (int, float)) or isinstance(ratio.get(key), bool):
            raise ValueError(f"QA 策略 length_ratio.{key} 必须是数字")
    if ratio["min"] < 0 or ratio["max"] < ratio["min"] or ratio["min_source_chars"] < 0:
        raise ValueError("QA 策略 length_ratio 数值范围无效")

    ignored = policy.get("ignore_rule_ids")
    if not isinstance(ignored, list) or not all(isinstance(value, str) for value in ignored):
        raise ValueError("QA 策略 ignore_rule_ids 必须是字符串数组")
    unknown_ignored = set(ignored) - _RULE_IDS
    if unknown_ignored:
        raise ValueError(f"QA 策略 ignore_rule_ids 含未知规则: {sorted(unknown_ignored)}")

    overrides = policy.get("entry_overrides")
    if not isinstance(overrides, dict):
        raise ValueError("QA 策略 entry_overrides 必须是对象")
    for entry_id, override in overrides.items():
        if not isinstance(override, dict):
            raise ValueError(f"QA entry_overrides.{entry_id} 必须是对象")
        unknown_keys = set(override) - _ENTRY_OVERRIDE_KEYS
        if unknown_keys:
            raise ValueError(f"QA entry_overrides.{entry_id} 含未知字段: {sorted(unknown_keys)}")
        disabled = override.get("disabled_rules", [])
        if not isinstance(disabled, list) or not all(isinstance(value, str) for value in disabled):
            raise ValueError(f"QA entry_overrides.{entry_id}.disabled_rules 必须是字符串数组")
        if set(disabled) - _RULE_IDS:
            raise ValueError(f"QA entry_overrides.{entry_id} 含未知规则")
        entry_rules = override.get("rules", {})
        if not isinstance(entry_rules, dict) or set(entry_rules) - _RULE_IDS:
            raise ValueError(f"QA entry_overrides.{entry_id}.rules 无效")
        for rule_id, config in entry_rules.items():
            if not isinstance(config, dict) or set(config) - _RULE_KEYS:
                raise ValueError(f"QA entry_overrides.{entry_id}.rules.{rule_id} 无效")
            if "enabled" in config and not isinstance(config["enabled"], bool):
                raise ValueError(f"QA entry_overrides.{entry_id}.rules.{rule_id}.enabled 必须是 boolean")
            if config.get("severity", "warning") not in _SEVERITIES:
                raise ValueError(f"QA entry_overrides.{entry_id}.rules.{rule_id}.severity 无效")
            if "comparison" in config and (
                rule_id != "number_parity"
                or config["comparison"] not in {"literal", "numeric_value"}
            ):
                raise ValueError(
                    f"QA entry_overrides.{entry_id}.rules.{rule_id}.comparison 无效"
                )

    plugin = policy.get("plugin")
    if plugin is not None:
        if not isinstance(plugin, dict) or set(plugin) - {"path", "timeout_seconds"}:
            raise ValueError("QA 策略 plugin 字段无效")
        if not isinstance(plugin.get("path"), str) or not plugin["path"].strip():
            raise ValueError("QA 策略 plugin.path 必须是非空字符串")
        timeout = plugin.get("timeout_seconds", 30)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
            raise ValueError("QA 策略 plugin.timeout_seconds 必须是 1 到 300 的整数")


def effective_qa_policy(policy: dict[str, Any], entry_id: str | int) -> dict[str, Any]:
    """Resolve rule enablement and severity for one entry."""
    _validate_qa_policy(policy)
    entry_id = str(entry_id)
    resolved = {
        "rules": {rule_id: dict(config) for rule_id, config in policy["rules"].items()},
        "protected_patterns": list(policy["protected_patterns"]),
        "length_ratio": dict(policy["length_ratio"]),
        "plugin": dict(policy["plugin"]) if policy.get("plugin") else None,
    }
    for rule_id in policy.get("ignore_rule_ids", []):
        resolved["rules"].setdefault(rule_id, {})["enabled"] = False
    override = policy.get("entry_overrides", {}).get(entry_id, {})
    for rule_id in override.get("disabled_rules", []):
        resolved["rules"].setdefault(rule_id, {})["enabled"] = False
    for rule_id, config in override.get("rules", {}).items():
        resolved["rules"].setdefault(rule_id, {}).update(config)
    return resolved


def _plain(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return _TAG_RE.sub("", text)


def _normal_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    try:
        from tm_store import display_tags
        text = display_tags(text)
    except ImportError:
        pass
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _finding_id(rule: str, entry_id: str, payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"{rule}:{entry_id}:{digest}"


def _make_finding(rule: str, severity: str, entry: dict, message: str, **extra) -> dict:
    entry_id = str(entry.get("id", ""))
    payload = {"message": message, **extra}
    return {
        "finding_id": _finding_id(rule, entry_id, payload),
        "id": entry_id,
        "rule": rule,
        "severity": severity,
        "message": message,
        **extra,
    }


def _rule_enabled(policy: dict[str, Any], entry: dict, rule_id: str) -> tuple[bool, str]:
    config = effective_qa_policy(policy, entry.get("id", ""))["rules"].get(rule_id, {})
    return bool(config.get("enabled", False)), str(config.get("severity", "warning"))


def _token_counter(text: str, patterns: list[str]) -> Counter[str]:
    values: list[str] = []
    for pattern in patterns:
        values.extend(re.findall(pattern, text))
    return Counter(values)


_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CHINESE_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_CHINESE_LARGE_UNITS = {"万": 10_000, "亿": 100_000_000, "兆": 1_000_000_000_000}


def _chinese_number_value(text: str) -> int | None:
    """Parse common integer Chinese numerals, including digit sequences."""
    value = text.rstrip("％%")
    if not value:
        return None
    if all(char in _CHINESE_DIGITS for char in value):
        return int("".join(str(_CHINESE_DIGITS[char]) for char in value))

    total = section = number = 0
    for char in value:
        if char in _CHINESE_DIGITS:
            number = _CHINESE_DIGITS[char]
        elif char in _CHINESE_SMALL_UNITS:
            section += (number or 1) * _CHINESE_SMALL_UNITS[char]
            number = 0
        elif char in _CHINESE_LARGE_UNITS:
            section += number
            total += (section or 1) * _CHINESE_LARGE_UNITS[char]
            section = number = 0
        else:
            return None
    return total + section + number


def _canonical_number(token: str) -> str:
    normalized = unicodedata.normalize("NFKC", token)
    percent = normalized.endswith("%")
    body = normalized[:-1] if percent else normalized
    if body.isdigit():
        value = str(int(body))
    else:
        parsed = _chinese_number_value(body)
        value = body if parsed is None else str(parsed)
    return f"{value}%" if percent else value


def _number_counter(
    text: str, allow_chinese: bool = False, numeric_value: bool = True,
) -> Counter[str]:
    normalized_text = unicodedata.normalize("NFKC", text)
    matches = list(_NUMBER_RE.finditer(normalized_text))
    result = Counter(
        _canonical_number(match.group(0)) if numeric_value else match.group(0)
        for match in matches
    )
    if not allow_chinese:
        return result
    occupied = [(match.start(), match.end()) for match in matches]
    for match in _CHINESE_PERCENT_RE.finditer(normalized_text):
        if any(match.start() < end and start < match.end() for start, end in occupied):
            continue
        parsed = _chinese_number_value(match.group(0)[3:])
        if parsed is not None:
            result[f"{parsed}%"] += 1
            occupied.append((match.start(), match.end()))
    for match in _CHINESE_NUMBER_RE.finditer(normalized_text):
        if any(match.start() < end and start < match.end() for start, end in occupied):
            continue
        canonical = _canonical_number(match.group(0))
        if canonical != match.group(0):
            result[canonical] += 1
    return result


def _brackets_balanced(text: str) -> bool:
    stack: list[str] = []
    for char in text:
        if char in _BRACKET_PAIRS:
            stack.append(_BRACKET_PAIRS[char])
        elif char in _BRACKET_CLOSES:
            if not stack or stack.pop() != char:
                return False
    return not stack


def _has_whitespace_issue(source: str, target: str) -> bool:
    if target != target.strip():
        return True
    if re.search(r"[ \t]{2,}", target) and not re.search(r"[ \t]{2,}", source):
        return True
    if re.search(r"[ \t]+[，。！？；：、》）】」』]", target):
        return True
    return False


def _append_if_enabled(findings: list[dict], policy: dict, entry: dict, rule: str, message: str, **extra):
    enabled, severity = _rule_enabled(policy, entry, rule)
    if enabled:
        findings.append(_make_finding(rule, severity, entry, message, **extra))


def run_qa_plugin(
    plugin: dict[str, Any] | None,
    results: list[dict],
    expected_entries: list[dict],
    existing_findings: list[dict],
) -> list[dict]:
    """Run an optional project QA plugin that can only add validated findings."""
    if not plugin:
        return []
    plugin_path = Path(plugin["path"])
    if not plugin_path.is_file():
        raise RuntimeError(f"QA 插件不存在: {plugin_path}")
    payload = {
        "schema_version": 1,
        "results": results,
        "entries": expected_entries,
        "default_findings": existing_findings,
    }
    env = {"PYTHONIOENCODING": "utf-8", "PATH": os.environ.get("PATH", "")}
    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(plugin_path)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=env,
            timeout=plugin.get("timeout_seconds", 30),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"QA 插件超时: {plugin_path}") from exc
    if completed.returncode:
        raise RuntimeError(
            f"QA 插件失败 ({completed.returncode}): {completed.stderr.strip()[:500]}"
        )
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("QA 插件 stdout 必须是 JSON 对象") from exc
    if not isinstance(output, dict) or set(output) != {"findings"}:
        raise RuntimeError("QA 插件输出只能包含 findings")
    raw_findings = output["findings"]
    if not isinstance(raw_findings, list):
        raise RuntimeError("QA 插件 findings 必须是数组")
    by_id = {str(entry.get("id")): entry for entry in expected_entries}
    findings = []
    for index, raw in enumerate(raw_findings):
        if not isinstance(raw, dict) or set(raw) - {"id", "rule", "severity", "message", "details"}:
            raise RuntimeError(f"QA 插件 finding #{index + 1} 字段无效")
        entry_id = str(raw.get("id", ""))
        if entry_id not in by_id:
            raise RuntimeError(f"QA 插件 finding #{index + 1} 引用了批外 id")
        rule = raw.get("rule")
        severity = raw.get("severity")
        message = raw.get("message")
        if not isinstance(rule, str) or not rule.strip() or severity not in _SEVERITIES or not isinstance(message, str):
            raise RuntimeError(f"QA 插件 finding #{index + 1} 内容无效")
        details = raw.get("details", {})
        if not isinstance(details, dict):
            raise RuntimeError(f"QA 插件 finding #{index + 1}.details 必须是对象")
        finding = _make_finding(
            f"plugin:{rule}", severity, by_id[entry_id], message,
            plugin_rule=rule, **details,
        )
        finding["plugin"] = True
        findings.append(finding)
    return findings


def run_qa(
    results: list[dict],
    expected_entries: list[dict],
    policy: dict[str, Any] | None = None,
    all_entries: list[dict] | None = None,
) -> dict[str, Any]:
    """Return deterministic QA findings without changing any input objects."""
    policy = policy or load_qa_policy()
    _validate_qa_policy(policy)
    result_map = {str(item.get("id")): item for item in results if isinstance(item, dict)}
    entries = []
    for expected in expected_entries:
        item = dict(expected)
        submitted = result_map.get(str(expected.get("id")), {})
        item["target"] = submitted.get("target", expected.get("target", expected.get("translated", "")))
        entries.append(item)

    findings: list[dict] = []
    for entry in entries:
        if entry.get("locked") or entry.get("source_locked"):
            continue
        source = entry.get("source", "") or ""
        target = entry.get("target", "") or ""
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        source_plain = _plain(source)
        target_plain = _plain(target)

        enabled, severity = _rule_enabled(policy, entry, "tm_exact_target_mismatch")
        if enabled:
            permanent_exact = []
            for match in entry.get("tm_matches", []) or []:
                try:
                    similarity = float(match.get("similarity", 0))
                except (TypeError, ValueError):
                    continue
                if similarity == 1.0 and str(match.get("target", "")).strip():
                    permanent_exact.append(match)
            # A permanent exact match is authoritative. Runtime matches are
            # consulted only when the permanent layer has no exact candidate.
            exact = permanent_exact
            tm_scope = "permanent"
            if not exact:
                exact = []
                tm_scope = "runtime"
                for match in entry.get("runtime_tm_matches", []) or []:
                    try:
                        similarity = float(match.get("similarity", 0))
                    except (TypeError, ValueError):
                        continue
                    if similarity == 1.0 and str(match.get("target", "")).strip():
                        exact.append(match)
            candidate_targets = sorted({_normal_text(match.get("target", "")) for match in exact})
            current = _normal_text(target)
            if candidate_targets and current not in candidate_targets:
                _append_if_enabled(
                    findings, policy, entry, "tm_exact_target_mismatch",
                    f"存在精确{tm_scope} TM 匹配，但当前译文与所有精确 TM 译文不一致",
                    tm_targets=candidate_targets, tm_scope=tm_scope,
                )
            elif len(candidate_targets) > 1:
                _append_if_enabled(
                    findings, policy, entry, "tm_exact_target_mismatch",
                    f"多个精确{tm_scope} TM 匹配之间的译文互相冲突",
                    tm_targets=candidate_targets, tm_scope=tm_scope,
                )

        patterns = effective_qa_policy(policy, entry.get("id", ""))["protected_patterns"]
        if _rule_enabled(policy, entry, "protected_token_parity")[0]:
            source_tokens = _token_counter(source_plain, patterns)
            target_tokens = _token_counter(target_plain, patterns)
            if source_tokens != target_tokens:
                _append_if_enabled(
                    findings, policy, entry, "protected_token_parity",
                    "占位符或转义序列与 source 不一致",
                    source_tokens=dict(source_tokens), target_tokens=dict(target_tokens),
                )

        number_config = effective_qa_policy(policy, entry.get("id", ""))["rules"].get(
            "number_parity", {}
        )
        if _rule_enabled(policy, entry, "number_parity")[0]:
            numeric_value = number_config.get("comparison", "numeric_value") == "numeric_value"
            source_numbers = _number_counter(source_plain, numeric_value=numeric_value)
            target_numbers = _number_counter(
                target_plain, allow_chinese=bool(source_numbers) and numeric_value,
                numeric_value=numeric_value,
            )
            if source_numbers != target_numbers:
                _append_if_enabled(
                    findings, policy, entry, "number_parity",
                    "数字或百分比与 source 不一致",
                    source_numbers=dict(source_numbers), target_numbers=dict(target_numbers),
                    comparison=number_config.get("comparison", "numeric_value"),
                )

        if _rule_enabled(policy, entry, "url_email_parity")[0]:
            source_urls = Counter(_URL_EMAIL_RE.findall(source_plain))
            target_urls = Counter(_URL_EMAIL_RE.findall(target_plain))
            if source_urls != target_urls:
                _append_if_enabled(
                    findings, policy, entry, "url_email_parity",
                    "URL 或邮箱与 source 不一致",
                    source_tokens=dict(source_urls), target_tokens=dict(target_urls),
                )

        source_newlines = _normal_text(source_plain).count("\n")
        target_newlines = _normal_text(target_plain).count("\n")
        if source_newlines != target_newlines:
            _append_if_enabled(
                findings, policy, entry, "newline_count",
                "译文换行数量与 source 不一致",
                source_newlines=source_newlines,
                target_newlines=target_newlines,
            )

        validation = entry.get("validation") if isinstance(entry.get("validation"), dict) else {}
        newline_policy = validation.get("newline_policy") if isinstance(validation, dict) else None
        if not isinstance(newline_policy, dict):
            newline_policy = {
                "mode": "exact" if validation.get("enforce_newline_count") else "source_guided",
                "preserve_paragraphs": True,
                "forbid_edge_breaks": True,
            }
        for issue in validate_newline_semantics(source, target, newline_policy):
            _append_if_enabled(
                findings, policy, entry, "newline_semantics", "译文换行布局无效: " + issue,
                layout=layout_preview(source, target, newline_policy),
            )
        for issue in suspicious_newline_warnings(target):
            _append_if_enabled(
                findings, policy, entry, "newline_suspicious_break", "译文换行位置可疑: " + issue,
                layout=layout_preview(source, target, newline_policy),
            )

        if _has_whitespace_issue(source_plain, target_plain):
            _append_if_enabled(
                findings, policy, entry, "whitespace",
                "译文包含可疑首尾空格、重复空格或中文标点前空格",
            )

        if not _brackets_balanced(target_plain):
            _append_if_enabled(
                findings, policy, entry, "punctuation_balance",
                "译文括号或引号未成对闭合",
            )

        if source_plain and _normal_text(source) == _normal_text(target) and _JAPANESE_RE.search(source_plain):
            _append_if_enabled(
                findings, policy, entry, "untranslated",
                "译文与 source 完全相同，疑似未翻译",
            )
        elif _JAPANESE_RE.search(target_plain) and not re.search(r"[\u4e00-\u9fff]", target_plain):
            _append_if_enabled(
                findings, policy, entry, "untranslated",
                "译文仍含较多日文字符，疑似存在漏译",
            )

        ratio_config = effective_qa_policy(policy, entry.get("id", ""))["length_ratio"]
        source_len = len(source_plain.strip())
        target_len = len(target_plain.strip())
        if source_len >= ratio_config["min_source_chars"] and source_len > 0:
            ratio = target_len / source_len
            if ratio < ratio_config["min"] or ratio > ratio_config["max"]:
                _append_if_enabled(
                    findings, policy, entry, "length_ratio",
                    f"译文长度比例异常: {ratio:.2f}",
                    ratio=round(ratio, 4), min=ratio_config["min"], max=ratio_config["max"],
                )

        if _rule_enabled(policy, entry, "term_consistency")[0]:
            missing_terms = []
            for term in entry.get("terms", []) or []:
                zh = str(term.get("zh", "")).strip() if isinstance(term, dict) else ""
                if zh and zh not in target_plain:
                    missing_terms.append(zh)
            if missing_terms:
                _append_if_enabled(
                    findings, policy, entry, "term_consistency",
                    "术语库译法未出现在译文中",
                    missing_terms=missing_terms,
                )

    if all_entries is not None:
        grouped: dict[str, set[str]] = defaultdict(set)
        for item in all_entries:
            source = _normal_text(item.get("source", ""))
            target = _normal_text(item.get("target", ""))
            if source and target and not item.get("locked") and not item.get("source_locked"):
                grouped[source].add(target)
        conflicts = {source: sorted(targets) for source, targets in grouped.items() if len(targets) > 1}
        for entry in entries:
            if entry.get("locked") or entry.get("source_locked"):
                continue
            source = _normal_text(entry.get("source", ""))
            if source in conflicts and _rule_enabled(
                policy, entry, "duplicate_consistency"
            )[0]:
                _append_if_enabled(
                    findings, policy, entry, "duplicate_consistency",
                    "相同 source 在项目中存在多个不同译法",
                    targets=conflicts[source],
                )

    ignored = set(policy.get("ignore_rule_ids", []))
    findings = [finding for finding in findings if finding["rule"] not in ignored]
    return {
        "schema_version": 1,
        "findings": findings,
        "summary": {
            "total": len(findings),
            "by_rule": dict(Counter(finding["rule"] for finding in findings)),
            "by_severity": dict(Counter(finding["severity"] for finding in findings)),
        },
    }


def validate_qa_report(
    report: Any,
    machine_findings: list[dict],
    baseline_results: list[dict],
    qa_results: list[dict],
) -> list[str]:
    """Validate QA agent decisions and target changes against machine findings."""
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["QA 报告必须是 JSON 对象"]
    if report.get("schema_version") != 1:
        errors.append("QA 报告 schema_version 必须为 1")
    decisions = report.get("findings")
    if not isinstance(decisions, list):
        return errors + ["QA 报告 findings 必须是数组"]

    expected_ids = [str(finding.get("finding_id")) for finding in machine_findings]
    expected_set = set(expected_ids)
    finding_entry_ids = {
        str(finding.get("finding_id")): str(finding.get("id"))
        for finding in machine_findings
    }
    finding_rules = {
        str(finding.get("finding_id")): str(finding.get("rule"))
        for finding in machine_findings
    }
    seen: set[str] = set()
    baseline_map = {str(item.get("id")): item.get("target", "") for item in baseline_results}
    result_map = {str(item.get("id")): item.get("target", "") for item in qa_results}
    statuses_by_entry: dict[str, set[str]] = defaultdict(set)
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        finding_id = str(decision.get("finding_id", ""))
        if finding_id in expected_set:
            statuses_by_entry[str(decision.get("id", ""))].add(
                str(decision.get("status", ""))
            )
    for decision in decisions:
        if not isinstance(decision, dict):
            errors.append("QA findings 中存在非对象")
            continue
        finding_id = str(decision.get("finding_id", ""))
        if finding_id in seen:
            errors.append(f"QA finding 重复: {finding_id}")
        seen.add(finding_id)
        if finding_id not in expected_set:
            errors.append(f"QA finding 未知或已过期: {finding_id}")
            continue
        status = decision.get("status")
        if status not in {"fixed", "false_positive"}:
            errors.append(f"QA finding 状态无效: {finding_id}: {status!r}")
            continue
        entry_id = str(decision.get("id", ""))
        expected_entry_id = finding_entry_ids[finding_id]
        if entry_id != expected_entry_id:
            errors.append(
                f"QA finding 对应的 id 不匹配: {finding_id}（预期 {expected_entry_id}，实际 {entry_id}）"
            )
            continue
        baseline = baseline_map.get(entry_id)
        current = result_map.get(entry_id)
        if baseline is None or current is None:
            errors.append(f"QA finding 对应的 id 不存在: {finding_id}")
            continue
        if status == "fixed" and current == baseline:
            errors.append(f"QA finding 标记 fixed 但 target 未改变: {finding_id}")
        if status == "false_positive" and finding_rules[finding_id] == "newline_semantics":
            errors.append(f"结构性换行 finding 必须修复，不能标记 false_positive: {finding_id}")
        if (
            status == "false_positive"
            and current != baseline
            and "fixed" not in statuses_by_entry.get(entry_id, set())
        ):
            errors.append(f"QA finding 标记 false_positive 但 target 被改变: {finding_id}")
        if not str(decision.get("reason", "")).strip():
            errors.append(f"QA finding 缺少判定理由: {finding_id}")
    missing = expected_set - seen
    if missing:
        errors.append(f"QA finding 未逐条处理: {sorted(missing)}")
    return errors
