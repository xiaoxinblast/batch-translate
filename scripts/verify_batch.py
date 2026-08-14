#!/usr/bin/env python3
"""批量翻译 - 批次提交前验证脚本。
用法: python batch_translate/scripts/verify_batch.py --stem <stem>
      或从 batch_translate/ 目录: python scripts/verify_batch.py --stem <stem>
"""

import argparse
import json
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows GBK 编码错误
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # batch_translate/
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from validation import load_validation_policy, validate_batch_results


def _check_batch(
    expected_ids: list[str],
    src_by_id: dict[str, str],
    data,
    allow_warnings: bool = False,
    policy: dict | None = None,
) -> tuple[list[str], list[str]]:
    """Compatibility wrapper used by unit tests."""
    expected = [
        {"id": str(entry_id), "source": src_by_id.get(str(entry_id), "")}
        for entry_id in expected_ids
    ]
    report = validate_batch_results(data, expected, policy)
    return report.fatal, report.warnings


def _load_expected_entries(
    state: dict, export_data: dict, stem: str | None = None
) -> list[dict]:
    """Prefer the generated batch task because it carries locked baselines."""
    batch_index = state["current_batch"]
    batch_num = batch_index + 1
    project_stem = state.get("stem") or stem
    project_exports = SCRIPT_DIR / "exports" / str(project_stem)
    for suffix in ("to_review", "to_translate"):
        task_path = project_exports / f"_batch_{batch_num:03d}_{suffix}.json"
        if task_path.is_file():
            with open(task_path, "r", encoding="utf-8") as f:
                task = json.load(f)
            if isinstance(task.get("entries"), list):
                return task["entries"]

    start, end = state["batches"][batch_index]
    entries = []
    for source_entry in export_data["entries"][start:end]:
        entry = dict(source_entry)
        entry["locked"] = bool(source_entry.get("source_locked"))
        entries.append(entry)
    return entries


def main():
    parser = argparse.ArgumentParser(description="验证提交前的 reviewed JSON")
    parser.add_argument("--stem", required=True, help="源文件 stem（不含扩展名）")
    parser.add_argument(
        "--allow-warnings",
        action="store_true",
        help="人工判定剩余警告可接受后放行",
    )
    parser.add_argument(
        "--warning-reason",
        default=None,
        help="放行 warning 的具体理由（与 --allow-warnings 同时使用）",
    )
    parser.add_argument(
        "--policy",
        default=None,
        help="项目验证策略 JSON（默认使用 state 中记录的路径或内置策略）",
    )
    args = parser.parse_args()
    if args.allow_warnings and not str(args.warning_reason or "").strip():
        print("FATAL: --allow-warnings 必须同时提供非空 --warning-reason")
        sys.exit(2)
    stem = args.stem

    # 加载状态
    state_path = SCRIPT_DIR / "data" / stem / "batch_state.json"
    if not state_path.is_file():
        print(f"FATAL: 状态文件不存在: {state_path}")
        sys.exit(2)

    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)

    bi = state["current_batch"]
    if bi >= len(state["batches"]):
        print("FATAL: 当前项目已没有待验证批次")
        sys.exit(2)
    batch_num = bi + 1

    # 加载 export 获取预期条目
    export_file = Path(state["export_file"])
    with open(export_file, encoding="utf-8") as f:
        export_data = json.load(f)

    expected_entries = _load_expected_entries(state, export_data, stem)

    # 加载 reviewed JSON
    reviewed_path = SCRIPT_DIR / "exports" / stem / f"_batch_{batch_num:03d}_reviewed.json"
    if not reviewed_path.is_file():
        print(f"FATAL: reviewed 文件不存在: {reviewed_path}")
        sys.exit(2)

    try:
        with open(reviewed_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        print(f"FATAL: reviewed JSON 解析失败: {exc}")
        sys.exit(1)

    try:
        policy = (
            load_validation_policy(args.policy)
            if args.policy
            else state.get("validation_policy")
            or load_validation_policy(state.get("validation_policy_path"))
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FATAL: 验证策略无效: {exc}")
        sys.exit(2)

    report = validate_batch_results(data, expected_entries, policy)
    if report.fatal:
        for message in report.fatal:
            print(f"FATAL: {message}")
        sys.exit(1)

    if report.warnings:
        print("WARNING:")
        for warning in report.warnings:
            print(f"  - {warning}")
        if args.allow_warnings:
            print(f"ACCEPTANCE REASON: {args.warning_reason.strip()}")
            print(
                f"RESULT: PASS (warnings accepted, {len(report.warnings)} accepted) "
                f"({len(report.entries)} entries, batch {batch_num}/{state['total_batches']})"
            )
        else:
            print(
                f"RESULT: BLOCKED with warnings ({len(report.entries)} entries, "
                f"batch {batch_num}/{state['total_batches']})"
            )
            sys.exit(3)
        sys.exit(0)

    print(
        f"RESULT: PASS ({len(report.entries)} entries, "
        f"batch {batch_num}/{state['total_batches']})"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
