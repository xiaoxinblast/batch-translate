#!/usr/bin/env python3
"""Layout metadata and semantic validation for inline line-break tags.

The module intentionally knows nothing about a particular game or format.  It
only treats ``type='br'`` tags and literal newlines as layout markers, keeping
the same interpretation in task generation, hard validation, and QA.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


TAG_TOKEN_RE = re.compile(r"<tag\b[^<>]*/>")
TAG_ATTR_RE = re.compile(r"\b(id|type|desc)\s*=\s*(['\"])(.*?)\2")


@dataclass(frozen=True)
class Break:
    """One visual break, optionally backed by an inline tag id."""

    tag_id: str | None
    start: int
    end: int


def _tag_attrs(token: str) -> dict[str, str]:
    return {match.group(1): match.group(3) for match in TAG_ATTR_RE.finditer(token)}


def _breaks(text: str) -> list[Break]:
    """Return literal and ``br`` breaks in their rendered order."""
    breaks: list[Break] = []
    cursor = 0
    for match in TAG_TOKEN_RE.finditer(text):
        for newline in re.finditer(r"\n", text[cursor:match.start()]):
            start = cursor + newline.start()
            breaks.append(Break(None, start, start + 1))
        attrs = _tag_attrs(match.group(0))
        if attrs.get("type") == "br":
            breaks.append(Break(attrs.get("id"), match.start(), match.end()))
        cursor = match.end()
    for newline in re.finditer(r"\n", text[cursor:]):
        start = cursor + newline.start()
        breaks.append(Break(None, start, start + 1))
    return breaks


def _rendered_text(text: str) -> str:
    """Render all tags as boundaries so no semantic check crosses a tag."""
    def replace(match: re.Match[str]) -> str:
        attrs = _tag_attrs(match.group(0))
        return "\n" if attrs.get("type") == "br" else "\u2028"

    return TAG_TOKEN_RE.sub(replace, text)


def _runs(text: str) -> list[list[Break]]:
    """Return consecutive break runs with only whitespace between markers."""
    values = _breaks(text)
    if not values:
        return []
    runs: list[list[Break]] = [[values[0]]]
    for item in values[1:]:
        between = text[runs[-1][-1].end:item.start]
        if not between.strip():
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def layout_metadata(source: str, policy: dict[str, Any]) -> dict[str, Any] | None:
    """Return compact, read-only layout data for tasks with line breaks."""
    if not _breaks(source):
        return None
    runs = _runs(source)
    rendered = _rendered_text(source)
    source_lines = rendered.split("\n")
    soft_breaks = []
    hard_paragraph_breaks = []
    line_index = 0
    for run in runs:
        if len(run) == 1:
            item = run[0]
            soft_breaks.append({
                "tag_id": item.tag_id,
                "after_source_line": line_index,
                "before_source_line": line_index + 1,
            })
        else:
            hard_paragraph_breaks.append({
                "tag_ids": [item.tag_id for item in run if item.tag_id],
                "after_source_line": line_index,
                "before_source_line": line_index + len(run),
            })
        line_index += len(run)
    return {
        "newline_policy": dict(policy),
        "source_lines": source_lines,
        "source_rendered_lines": source_lines,
        "soft_breaks": soft_breaks,
        "soft_break_tag_ids": [item["tag_id"] for item in soft_breaks if item["tag_id"]],
        "hard_paragraph_breaks": hard_paragraph_breaks,
        "max_consecutive_breaks": max((len(run) for run in runs), default=0),
    }


def layout_preview(source: str, target: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Human-readable source/target rendering included in QA tasks."""
    return {
        "policy": dict(policy),
        "source": _rendered_text(source).split("\n"),
        "target": _rendered_text(target).split("\n"),
    }


def validate_newline_semantics(
    source: str, target: str, policy: dict[str, Any],
) -> list[str]:
    """Validate the semantic invariants of flexible inline line wrapping."""
    mode = policy.get("mode", "source_guided")
    source_breaks = _breaks(source)
    target_breaks = _breaks(target)
    if not source_breaks and not target_breaks:
        return []

    errors: list[str] = []
    source_ids = [item.tag_id for item in source_breaks if item.tag_id]
    target_ids = [item.tag_id for item in target_breaks if item.tag_id]
    unknown = [tag_id for tag_id in target_ids if tag_id not in source_ids]
    if unknown:
        errors.append(f"target 含 source 没有的 br 标签 id: {sorted(set(unknown))}")
    for tag_id in sorted(set(target_ids)):
        if target_ids.count(tag_id) > 1:
            errors.append(f"target 重复使用 br 标签 id: {tag_id}")

    # source_guided can omit soft breaks, but an adopted break may not jump
    # across another source break or a hard paragraph boundary.
    source_cursor = 0
    for tag_id in target_ids:
        try:
            source_cursor = source_ids.index(tag_id, source_cursor) + 1
        except ValueError:
            errors.append("target 的 br 标签顺序与 source 不一致")
            break

    if policy.get("forbid_edge_breaks", True) and target_breaks:
        first, last = target_breaks[0], target_breaks[-1]
        if not _rendered_text(target[:first.start]).strip():
            errors.append("target 以换行开头")
        if not _rendered_text(target[last.end:]).strip():
            errors.append("target 以换行结尾")

    source_runs = _runs(source)
    target_runs = _runs(target)
    if mode != "free":
        source_max = max((len(run) for run in source_runs), default=0)
        target_max = max((len(run) for run in target_runs), default=0)
        if target_max > source_max:
            errors.append(
                f"target 连续换行数 {target_max} 超过 source 的 {source_max}"
            )

        hard_groups = [run for run in source_runs if len(run) >= 2]
        if policy.get("preserve_paragraphs", True):
            for group in hard_groups:
                required_ids = [item.tag_id for item in group if item.tag_id]
                if not required_ids:
                    continue
                found = any(
                    [item.tag_id for item in run if item.tag_id] == required_ids
                    for run in target_runs
                )
                if not found:
                    errors.append(
                        "target 未保留 source 的硬段落边界: " + ",".join(required_ids)
                    )
            if len([run for run in target_runs if len(run) >= 2]) != len(hard_groups):
                errors.append("target 的硬段落数量与 source 不一致")

    if mode == "exact":
        if target_ids != source_ids or len(target_breaks) != len(source_breaks):
            errors.append("exact 模式要求 target 的换行标签和数量与 source 一致")
    return errors


def suspicious_newline_warnings(target: str) -> list[str]:
    """Return review-only warnings for likely accidental target line breaks."""
    rendered = _rendered_text(target)
    warnings: list[str] = []
    for index in range(1, len(rendered) - 1):
        if rendered[index] != "\n":
            continue
        before, after = rendered[index - 1], rendered[index + 1]
        if before.isdigit() and after.isdigit():
            warnings.append("换行断在数字内部")
        elif re.match(r"[A-Za-z0-9_]", before) and re.match(r"[A-Za-z0-9_]", after):
            warnings.append("换行断在英文标识内部")
    for line in rendered.split("\n")[1:]:
        stripped = line.strip()
        if stripped and re.fullmatch(r"[\s\.,，。！？；：、…!?;:）】」』》]+", stripped):
            warnings.append("换行后只剩标点")
    return list(dict.fromkeys(warnings))
