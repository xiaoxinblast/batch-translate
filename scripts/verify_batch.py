#!/usr/bin/env python3
"""批量翻译 - 批次提交前验证脚本。
用法: python batch_translate/scripts/verify_batch.py --stem <stem>
      或从 batch_translate/ 目录: python scripts/verify_batch.py --stem <stem>
"""

import argparse
import json
import re
import sys
from pathlib import Path

# 强制 UTF-8 输出，避免 Windows GBK 编码错误
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TAG_RE = re.compile(r"""<tag\s+id=['"][^'"]+['"].*?/>""")
SCRIPT_DIR = Path(__file__).resolve().parent.parent  # batch_translate/


def main():
    parser = argparse.ArgumentParser(description="验证提交前的 reviewed JSON")
    parser.add_argument("--stem", required=True, help="源文件 stem（不含扩展名）")
    args = parser.parse_args()
    stem = args.stem

    # 加载状态
    state_path = SCRIPT_DIR / "data" / stem / "batch_state.json"
    if not state_path.is_file():
        print(f"FATAL: 状态文件不存在: {state_path}")
        sys.exit(2)

    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)

    bi = state["current_batch"]
    start, end = state["batches"][bi]
    batch_num = bi + 1

    # 加载 export 获取预期条目
    export_file = Path(state["export_file"])
    with open(export_file, encoding="utf-8") as f:
        export_data = json.load(f)

    batch_entries = export_data["entries"][start:end]
    expected_ids = [str(e["id"]) for e in batch_entries]
    src_by_id = {str(e["id"]): e.get("source", "") for e in batch_entries}

    # 加载 reviewed JSON
    reviewed_path = SCRIPT_DIR / "exports" / stem / f"_batch_{batch_num:03d}_reviewed.json"
    if not reviewed_path.is_file():
        print(f"FATAL: reviewed 文件不存在: {reviewed_path}")
        sys.exit(2)

    with open(reviewed_path, encoding="utf-8") as f:
        data = json.load(f)

    # 容错：自动解包 {"entries": [...]}
    if isinstance(data, dict) and "entries" in data:
        print("WARNING: wrapped object {entries:[...]}, auto-unwrapped")
        data = data["entries"]

    if not isinstance(data, list):
        print("FATAL: reviewed JSON 不是数组 → 退回校对步骤重跑")
        sys.exit(1)

    submitted_ids = [str(r.get("id")) for r in data]
    sub_set = set(submitted_ids)
    exp_set = set(expected_ids)

    missing = exp_set - sub_set
    extra = sub_set - exp_set

    # ── 致命：条数不符 / id 未全覆盖 ──
    if len(data) != len(expected_ids) or missing:
        print(f"FATAL: 条数不符：预期 {len(expected_ids)}，实际 {len(data)}")
        if missing:
            print(f"   缺失 id（前 20）：{sorted(missing)[:20]}")
        print("   → 校对可能只输出了改动条，请要求输出本批全部条目后重跑")
        sys.exit(1)

    # ── 警告：标签数不一致 ──
    tag_bad = []
    for r in data:
        rid = str(r["id"])
        src = src_by_id.get(rid, "")
        tgt = r.get("target") or ""
        if "<tag" in src and len(TAG_RE.findall(src)) != len(TAG_RE.findall(tgt)):
            tag_bad.append(rid)

    # ── 警告：多余 id ──
    extra_list = sorted(extra) if extra else []

    # ── 汇总 ──
    warnings = []
    if tag_bad:
        warnings.append(f"{len(tag_bad)} 条标签数与 source 不一致: {tag_bad[:20]}")
    if extra_list:
        warnings.append(f"{len(extra_list)} 条 id 不属于本批: {extra_list[:20]}")

    if warnings:
        print("WARNING:")
        for w in warnings:
            print(f"  - {w}")
        print(f"RESULT: PASS with warnings ({len(data)} entries, batch {batch_num}/{state['total_batches']})")
        sys.exit(0)
    else:
        print(f"RESULT: PASS ({len(data)} entries, batch {batch_num}/{state['total_batches']})")
        sys.exit(0)


if __name__ == "__main__":
    main()
