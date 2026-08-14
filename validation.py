#!/usr/bin/env python3
"""Shared batch-result validation for verify_batch.py and batch.py submit."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TAG_TOKEN_RE = re.compile(r"<tag\b[^<>]*/>")
TAG_ATTR_RE = re.compile(r"\b(id|type|desc)\s*=\s*(['\"])(.*?)\2")
BARE_TAG_RE = re.compile(r"<(?!tag\b)(/?[a-zA-Z][a-zA-Z0-9]*(?:=[^>]*)?)>")

DEFAULT_POLICY: dict[str, Any] = {
    "ignored_tag_types": ["br"],
    "tag_mode": "exact",
    "enforce_maxlength": True,
    "enforce_newline_count": False,
    "allow_empty_ids": [],
    "entry_overrides": {},
}

_POLICY_KEYS = set(DEFAULT_POLICY)
_OVERRIDE_KEYS = {
    "ignored_tag_types",
    "tag_mode",
    "enforce_maxlength",
    "enforce_newline_count",
    "allow_empty",
}


@dataclass
class ValidationResult:
    entries: list[dict] = field(default_factory=list)
    fatal: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fatal


def load_validation_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load an optional project policy and merge it over safe defaults."""
    policy = {
        **DEFAULT_POLICY,
        "ignored_tag_types": list(DEFAULT_POLICY["ignored_tag_types"]),
        "allow_empty_ids": list(DEFAULT_POLICY["allow_empty_ids"]),
        "entry_overrides": {},
    }
    if not path:
        return policy

    policy_path = Path(path)
    if not policy_path.is_file():
        raise FileNotFoundError(f"验证策略不存在: {policy_path}")
    with open(policy_path, "r", encoding="utf-8") as f:
        custom = json.load(f)
    if not isinstance(custom, dict):
        raise ValueError("验证策略必须是 JSON 对象")

    unknown = set(custom) - _POLICY_KEYS
    if unknown:
        raise ValueError(f"验证策略含未知字段: {sorted(unknown)}")
    policy.update(custom)
    _validate_policy(policy)
    return policy


def validate_batch_results(
    data: Any,
    expected_entries: list[dict],
    policy: dict[str, Any] | None = None,
) -> ValidationResult:
    """Validate reviewed output against the exact current-batch contract."""
    result = ValidationResult()
    policy = policy or load_validation_policy()
    _validate_policy(policy)

    if isinstance(data, dict) and "entries" in data:
        result.warnings.append("输出包装为 {entries:[...]}，已自动解包")
        data = data["entries"]
    if not isinstance(data, list):
        result.fatal.append("reviewed JSON 必须是数组")
        return result

    result.entries = data
    submitted: dict[str, dict] = {}
    seen: set[str] = set()
    duplicates: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            result.fatal.append(f"第 {index + 1} 条不是 JSON 对象")
            continue
        if "id" not in item or "target" not in item:
            missing = [key for key in ("id", "target") if key not in item]
            result.fatal.append(f"第 {index + 1} 条缺少字段: {missing}")
            continue
        if not isinstance(item["id"], str):
            result.fatal.append(f"第 {index + 1} 条 id 必须是字符串")
            continue
        if not isinstance(item["target"], str):
            result.fatal.append(f"id={item['id']} 的 target 必须是字符串")
            continue
        item_id = item["id"]
        if item_id in seen:
            duplicates.add(item_id)
        seen.add(item_id)
        submitted[item_id] = item

    expected_ids = [str(entry["id"]) for entry in expected_entries]
    expected_set = set(expected_ids)
    submitted_set = set(submitted)
    missing_ids = expected_set - submitted_set
    extra_ids = submitted_set - expected_set
    if len(data) != len(expected_entries):
        result.fatal.append(
            f"条数不符: 预期 {len(expected_entries)}，实际 {len(data)}"
        )
    if duplicates:
        result.fatal.append(f"存在重复 id: {sorted(duplicates)[:20]}")
    if missing_ids:
        result.fatal.append(f"缺失 id: {sorted(missing_ids)[:20]}")
    if extra_ids:
        result.fatal.append(f"包含批外 id: {sorted(extra_ids)[:20]}")

    allow_empty_ids = {str(v) for v in policy.get("allow_empty_ids", [])}
    for expected in expected_entries:
        entry_id = str(expected["id"])
        submitted_item = submitted.get(entry_id)
        if submitted_item is None:
            continue
        target = submitted_item["target"]
        source = expected.get("source") or ""
        baseline = expected.get("translated", expected.get("target", "")) or ""
        entry_policy = _entry_policy(policy, entry_id)
        locked = bool(expected.get("locked"))
        source_locked = bool(expected.get("source_locked"))

        if locked and target != baseline:
            result.fatal.append(f"id={entry_id} 为 locked，target 被修改")

        allow_empty = (
            entry_id in allow_empty_ids
            or bool(entry_policy.get("allow_empty"))
            or (source_locked and baseline == "")
        )
        if not target.strip() and not allow_empty:
            result.fatal.append(f"id={entry_id} 的 target 为空")

        if BARE_TAG_RE.search(target):
            result.fatal.append(f"id={entry_id} 的 target 含非 <tag .../> 裸标签")

        source_tags, source_malformed = _extract_tags(source)
        target_tags, target_malformed = _extract_tags(target)
        if source_malformed:
            result.fatal.append(f"id={entry_id} 的 source 含无法解析的 <tag> 标记")
        if target_malformed:
            result.fatal.append(f"id={entry_id} 的 target 含无法解析的 <tag> 标记")

        ignored = set(entry_policy.get("ignored_tag_types", []))
        source_tags = [tag for tag in source_tags if tag[1] not in ignored]
        target_tags = [tag for tag in target_tags if tag[1] not in ignored]
        tag_mode = entry_policy.get("tag_mode", "exact")
        if tag_mode == "exact" and source_tags != target_tags:
            result.fatal.append(f"id={entry_id} 的标签序列与 source 不一致")
        elif tag_mode == "count" and len(source_tags) != len(target_tags):
            result.fatal.append(f"id={entry_id} 的标签数量与 source 不一致")

        if entry_policy.get("enforce_newline_count"):
            if source.count("\n") != target.count("\n"):
                result.fatal.append(f"id={entry_id} 的换行数量与 source 不一致")

        if entry_policy.get("enforce_maxlength") and expected.get("maxlengthchars"):
            try:
                max_length = int(expected["maxlengthchars"])
            except (TypeError, ValueError):
                result.warnings.append(
                    f"id={entry_id} 的 maxlengthchars 无效: {expected['maxlengthchars']}"
                )
            else:
                plain_target = TAG_TOKEN_RE.sub("", target)
                if max_length >= 0 and len(plain_target) > max_length:
                    result.fatal.append(
                        f"id={entry_id} 超出 maxlengthchars: {len(plain_target)} > {max_length}"
                    )

    return result


def _extract_tags(text: str) -> tuple[list[tuple[str, str, str]], bool]:
    tags: list[tuple[str, str, str]] = []
    for token in TAG_TOKEN_RE.findall(text):
        attrs = {match.group(1): match.group(3) for match in TAG_ATTR_RE.finditer(token)}
        if "id" not in attrs or "type" not in attrs:
            return tags, True
        tags.append((attrs["id"], attrs["type"], attrs.get("desc", "")))
    remainder = TAG_TOKEN_RE.sub("", text)
    return tags, "<tag" in remainder


def _entry_policy(policy: dict[str, Any], entry_id: str) -> dict[str, Any]:
    merged = {
        "ignored_tag_types": list(policy.get("ignored_tag_types", [])),
        "tag_mode": policy.get("tag_mode", "exact"),
        "enforce_maxlength": bool(policy.get("enforce_maxlength", True)),
        "enforce_newline_count": bool(policy.get("enforce_newline_count", False)),
        "allow_empty": False,
    }
    override = policy.get("entry_overrides", {}).get(entry_id, {})
    merged.update(override)
    return merged


def _validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("tag_mode") not in {"exact", "count", "ignore"}:
        raise ValueError("tag_mode 必须是 exact、count 或 ignore")
    ignored = policy.get("ignored_tag_types", [])
    if not isinstance(ignored, list) or not all(
        isinstance(value, str) for value in ignored
    ):
        raise ValueError("ignored_tag_types 必须是数组")
    allow_empty_ids = policy.get("allow_empty_ids", [])
    if not isinstance(allow_empty_ids, list) or not all(
        isinstance(value, (str, int)) and not isinstance(value, bool)
        for value in allow_empty_ids
    ):
        raise ValueError("allow_empty_ids 必须是数组")
    for key in ("enforce_maxlength", "enforce_newline_count"):
        if not isinstance(policy.get(key), bool):
            raise ValueError(f"{key} 必须是 JSON boolean")
    overrides = policy.get("entry_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("entry_overrides 必须是对象")
    for entry_id, override in overrides.items():
        if not isinstance(override, dict):
            raise ValueError(f"entry_overrides.{entry_id} 必须是对象")
        unknown = set(override) - _OVERRIDE_KEYS
        if unknown:
            raise ValueError(
                f"entry_overrides.{entry_id} 含未知字段: {sorted(unknown)}"
            )
        if override.get("tag_mode", "exact") not in {"exact", "count", "ignore"}:
            raise ValueError(
                f"entry_overrides.{entry_id}.tag_mode 必须是 exact、count 或 ignore"
            )
        if "ignored_tag_types" in override:
            value = override["ignored_tag_types"]
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError(
                    f"entry_overrides.{entry_id}.ignored_tag_types 必须是数组"
                )
        for key in ("enforce_maxlength", "enforce_newline_count", "allow_empty"):
            if key in override and not isinstance(override[key], bool):
                raise ValueError(
                    f"entry_overrides.{entry_id}.{key} 必须是 JSON boolean"
                )
