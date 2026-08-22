#!/usr/bin/env python3
"""Versioned, project-wide runtime rules for the batch translation toolkit."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProjectRulesError(ValueError):
    pass


def rules_root(toolkit_dir: Path) -> Path:
    return toolkit_dir / "data" / "project_rules"


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def revision_id(validation_policy: dict, qa_policy: dict, plugin_bytes: bytes | None) -> str:
    digest = hashlib.sha256()
    digest.update(_canonical_bytes(validation_policy))
    digest.update(b"\0")
    digest.update(_canonical_bytes(qa_policy))
    digest.update(b"\0")
    digest.update(plugin_bytes or b"")
    return digest.hexdigest()


def current(toolkit_dir: Path) -> dict | None:
    path = rules_root(toolkit_dir) / "current.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectRulesError(f"项目规则 current.json 无效: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ProjectRulesError("项目规则 current.json schema 无效")
    _validate_record(toolkit_dir, value)
    return value


def _validate_record(toolkit_dir: Path, record: dict) -> None:
    revision = record.get("revision")
    if not isinstance(revision, str) or len(revision) != 64:
        raise ProjectRulesError("项目规则 revision 无效")
    expected_dir = rules_root(toolkit_dir) / "revisions" / revision
    for key, name in (("validation_policy_path", "validation_policy.json"), ("qa_policy_path", "qa_policy.json")):
        value = record.get(key)
        if not isinstance(value, str) or Path(value).resolve() != (expected_dir / name).resolve():
            raise ProjectRulesError(f"项目规则 {key} 无效")
        if not (expected_dir / name).is_file():
            raise ProjectRulesError(f"项目规则文件不存在: {expected_dir / name}")
    plugin = record.get("qa_plugin_path")
    if plugin is not None and (
        not isinstance(plugin, str) or Path(plugin).resolve() != (expected_dir / "qa_plugin.py").resolve()
        or not (expected_dir / "qa_plugin.py").is_file()
    ):
        raise ProjectRulesError("项目规则 qa_plugin_path 无效")


def create_revision(
    toolkit_dir: Path,
    validation_policy: dict,
    qa_policy: dict,
    *,
    plugin_path: Path | None = None,
    sources: dict[str, str | None] | None = None,
) -> dict:
    """Create an immutable revision, returning its manifest record."""
    plugin_bytes = plugin_path.read_bytes() if plugin_path else None
    if plugin_path and not plugin_path.is_file():
        raise ProjectRulesError(f"QA 插件不存在: {plugin_path}")
    revision = revision_id(validation_policy, qa_policy, plugin_bytes)
    directory = rules_root(toolkit_dir) / "revisions" / revision
    validation_path = directory / "validation_policy.json"
    qa_path = directory / "qa_policy.json"
    plugin_destination = directory / "qa_plugin.py"
    if not directory.exists():
        _write_json_atomic(validation_path, validation_policy)
        _write_json_atomic(qa_path, qa_policy)
        if plugin_bytes is not None:
            plugin_destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".qa_plugin.", suffix=".tmp", dir=plugin_destination.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(plugin_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, plugin_destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
    record = {
        "schema_version": 1,
        "revision": revision,
        "validation_policy_path": str(validation_path.resolve()),
        "qa_policy_path": str(qa_path.resolve()),
        "qa_plugin_path": str(plugin_destination.resolve()) if plugin_bytes is not None else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources or {},
    }
    _write_json_atomic(directory / "revision.json", record)
    return record


def set_current(toolkit_dir: Path, record: dict) -> dict:
    _validate_record(toolkit_dir, record)
    _write_json_atomic(rules_root(toolkit_dir) / "current.json", record)
    return record


def ensure_current(
    toolkit_dir: Path,
    validation_policy: dict | None,
    qa_policy: dict | None,
    *,
    plugin_path: Path | None = None,
    sources: dict[str, str | None] | None = None,
) -> dict:
    """Get the active revision or create it once from explicitly supplied rules."""
    existing = current(toolkit_dir)
    if existing is None:
        if validation_policy is None or qa_policy is None:
            raise ProjectRulesError("项目规则尚未初始化，请使用 project-config init")
        return set_current(
            toolkit_dir,
            create_revision(
                toolkit_dir, validation_policy, qa_policy,
                plugin_path=plugin_path, sources=sources,
            ),
        )
    if validation_policy is None and qa_policy is None and plugin_path is None:
        return existing
    if validation_policy is None or qa_policy is None:
        raise ProjectRulesError("指定项目规则时必须同时提供 validation 和 QA 策略")
    desired = create_revision(
        toolkit_dir, validation_policy, qa_policy, plugin_path=plugin_path, sources=sources,
    )
    if desired["revision"] != existing["revision"]:
        raise ProjectRulesError(
            "init 参数与当前共享项目规则不同，请先使用 project-config update"
        )
    return existing
