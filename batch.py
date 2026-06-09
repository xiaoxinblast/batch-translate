#!/usr/bin/env python3
"""
批量翻译工作流：export → 分批发给 AI → 合并 → import → 循环
用法:
  python batch_translate/batch.py init <mqxliff> --batch-size 30 --context-size 5 ...
  python batch_translate/batch.py next
  python batch_translate/batch.py submit <result.json>
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_SCRIPT_DIR = Path(__file__).resolve().parent
_STATE_FILE = _SCRIPT_DIR / "data" / "batch_state.json"
_DEFAULT_EXPORT = _SCRIPT_DIR / "exports" / "_working.json"


def _load_state() -> dict:
    if not _STATE_FILE.is_file():
        print("❌ 未初始化，请先运行 init")
        sys.exit(1)
    with open(_STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict):
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════════

def _accumulate_tm(export_file: Path, tm_path: str):
    """非 mqxliff 格式的 TM 积累。"""
    try:
        from tm_store import TranslationMemory
    except ImportError:
        return
    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    tm = TranslationMemory(tm_path)
    entries = []
    for e in data.get("entries", []):
        tgt = e.get("target", "").strip()
        src = e.get("source", "").strip()
        if tgt and src:
            entries.append({
                "source": src,
                "target": tgt,
                "context": e.get("context", ""),
                "file": data.get("source_file", ""),
            })
    if entries:
        tm.add(entries)
        tm.save()


def _enrich_working_json(json_path: Path, state: dict):
    """对工作 JSON 做术语/TM/风格指南增强。"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # style_guide（所有格式都需要嵌入，供 review-only 模式使用）
    sg_path = state.get("style_guide_path")
    if sg_path and not data.get("style_guide"):
        sg = Path(sg_path)
        if sg.is_file():
            data["style_guide"] = sg.read_text(encoding="utf-8")

    if state["source_format"] == "mqxliff":
        # TM/terms 由 mqxliff_tool.py export 已做，只需补 style_guide
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return

    # terms
    terms_path = state.get("terms_path")
    if terms_path:
        try:
            from term_base import TermBase
            tb = TermBase(terms_path)
            tb.load()
            import re
            tr = re.compile(r"<tag[^>]*/>")
            for e in data.get("entries", []):
                plain = tr.sub("", e.get("source", ""))
                terms = tb.find_terms(plain)
                if terms:
                    e["terms"] = terms
        except ImportError:
            pass

    # TM
    tm_path = state.get("tm_path")
    if tm_path:
        try:
            from tm_store import TranslationMemory
            tm = TranslationMemory(tm_path)
            import re
            tr = re.compile(r"<tag[^>]*/>")
            for e in data.get("entries", []):
                plain = tr.sub("", e.get("source", ""))
                matches = tm.find_matches(plain)
                if matches:
                    e["tm_matches"] = matches
        except ImportError:
            pass

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _generate_summary(entries: list, batches: list, batch_chars: int) -> str:
    """生成文档结构摘要。"""
    import re
    _tag_re = re.compile(r"<tag[^>]*/>")

    total = len(entries)
    total_chars = sum(len(_tag_re.sub("", e["source"])) for e in entries)
    has_tags = sum(1 for e in entries if "<tag" in e["source"])

    # 按长度分类
    short = sum(1 for e in entries if len(_tag_re.sub("", e["source"])) <= 20)
    medium = sum(1 for e in entries if 20 < len(_tag_re.sub("", e["source"])) <= 80)
    long = sum(1 for e in entries if len(_tag_re.sub("", e["source"])) > 80)

    # 按 context 前缀分布
    ctx_groups = {}
    for e in entries:
        ctx = e.get("context", "") or ""
        prefix = ctx.split(".")[0] if "." in ctx else (ctx or "(无上下文)")
        ctx_groups[prefix] = ctx_groups.get(prefix, 0) + 1

    # 变量占位符
    has_vars = sum(1 for e in entries if "{0}" in e["source"] or "{1}" in e["source"])

    lines = [
        "━" * 40,
        "文档结构分析",
        "━" * 40,
        f"总条目: {total}  总字数: {total_chars}  批次: {len(batches)}（每批 ~{batch_chars} 字）",
        f"文本类型: 短文本(≤20字) {short}条 | 中等(21-80字) {medium}条 | 长文本(>80字) {long}条",
        f"内联标签: {has_tags} 条含标签  变量占位符: {has_vars} 条含 {{0}}/{{1}}",
        "",
        "上下文分布:",
    ]
    for prefix, count in sorted(ctx_groups.items(), key=lambda x: -x[1]):
        pct = count / total * 100
        lines.append(f"  {prefix}: {count} 条 ({pct:.0f}%)")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# init
# ═══════════════════════════════════════════════════════════════════════

def cmd_init(
    source_path: Path,
    batch_chars: int = 3000,
    context_size: int = 5,
    terms_path: Optional[Path] = None,
    tm_path: Optional[Path] = None,
    style_guide_path: Optional[Path] = None,
    source_col: str = "A",
    target_col: str = "B",
    header_row: int = 1,
):
    """初始化批量翻译：解析源文件 → 中间 JSON，写入 state。"""
    if _STATE_FILE.is_file():
        print("⚠️ 状态文件已存在，将覆盖。")
        print("  如需继续之前的任务，请直接运行 next")

    # 复制源文件到工作文件（不动源文件）
    import shutil
    work_file = _SCRIPT_DIR / "data" / f"_working_{source_path.name}"
    shutil.copy2(source_path, work_file)

    # 用 convert.py 解析
    export_file = _SCRIPT_DIR / "exports" / "_working.json"
    parse_args = [
        sys.executable, str(_SCRIPT_DIR / "convert.py"), "parse",
        str(work_file),
        "--output", str(export_file),
    ]
    if source_path.suffix.lower() in (".xlsx", ".xlsm"):
        parse_args += ["--source-col", source_col, "--target-col", target_col,
                       "--header-row", str(header_row)]

    import subprocess
    subprocess.run(parse_args, check=True)

    # 加载中间 JSON
    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    entries = data["entries"]
    total = len(entries)

    # 按字数分批
    import re
    _tag_re = re.compile(r"<tag[^>]*/>")
    batches = []
    start = 0
    cum = 0
    for i, e in enumerate(entries):
        plain = _tag_re.sub("", e["source"])
        char_len = len(plain)
        if cum + char_len > batch_chars and cum > 0:
            batches.append((start, i))
            start = i
            cum = 0
        cum += char_len
    if start < total:
        batches.append((start, total))
    total_batches = len(batches)

    # 加载 style_guide（如果 parse 阶段没加载）
    if not data.get("style_guide") and style_guide_path and style_guide_path.is_file():
        data["style_guide"] = style_guide_path.read_text(encoding="utf-8")

    # 生成文档摘要
    document_summary = _generate_summary(entries, batches, batch_chars)

    state = {
        "source_file": str(work_file.resolve()),
        "source_format": data.get("_format", source_path.suffix.lower().lstrip(".")),
        "export_file": str(export_file.resolve()),
        "total": total,
        "batch_chars": batch_chars,
        "context_size": context_size,
        "total_batches": total_batches,
        "batches": batches,
        "current_batch": 0,
        "document_summary": document_summary,
        "terms_path": str(terms_path.resolve()) if terms_path else None,
        "tm_path": str(tm_path.resolve()) if tm_path else None,
        "style_guide_path": str(style_guide_path.resolve()) if style_guide_path else None,
    }
    _save_state(state)

    # 显示分批信息
    avg = sum(e - s for s, e in batches) / total_batches
    print(f"✅ 初始化完成")
    print(f"   文件: {export_file.name}")
    print(f"   总数: {total} 条, 每批 ~{batch_chars} 字, 共 {total_batches} 批（平均 ~{avg:.0f} 条/批）")
    print(f"   上下文窗口: {context_size} 条")
    print()
    print(document_summary)
    print()
    print("运行 next 获取第一批翻译任务。")


# ═══════════════════════════════════════════════════════════════════════
# next
# ═══════════════════════════════════════════════════════════════════════

def cmd_next(review_only: bool = False):
    """输出当前批次的翻译 JSON（或校对 JSON，若 review_only=True）。"""
    state = _load_state()

    # 检查是否已完成
    batch_idx = state["current_batch"]
    batches = state["batches"]
    if batch_idx >= len(batches):
        print("✅ 全部翻译完成！")
        return

    # 加载 export
    export_file = Path(state["export_file"])
    if not export_file.is_file():
        print(f"❌ export 文件不存在: {export_file}")
        sys.exit(1)
    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries = data["entries"]
    total = state["total"]
    context_size = state["context_size"]
    start, end = batches[batch_idx]
    batch_num = batch_idx + 1

    # 上文：上一批末尾的 N 条已译条目
    context_entries = []
    if start > 0:
        ctx_start = max(0, start - context_size)
        for e in entries[ctx_start:start]:
            tgt = e.get("target", "").strip()
            if tgt:
                context_entries.append({
                    "id": e["id"],
                    "source": e["source"],
                    "target": tgt,
                })

    if review_only:
        # ── 校对模式：直接生成 review JSON（跳过翻译） ──
        out_path = _SCRIPT_DIR / "exports" / f"_batch_{batch_num:03d}_to_review.json"
        review = {}
        review["instructions"] = (
            "逐条核对译文与原文：1)术语是否准确统一 2)标点格式是否符合规范 "
            "3)语气是否符合角色 4)表达是否自然流畅、无翻译腔。"
            "发现问题直接修正，无需标注。"
        )
        if state.get("document_summary"):
            review["document_summary"] = state["document_summary"]
        if data.get("style_guide"):
            review["style_guide"] = data["style_guide"]
        if context_entries:
            review["previous"] = context_entries
        review["batch"] = batch_num
        review["total_batches"] = state["total_batches"]

        review_entries = []
        for e in entries[start:end]:
            item = {
                "id": e["id"],
                "source": e["source"],
                "translated": e.get("target", ""),
            }
            if e.get("context"):
                item["context"] = e["context"]
            if e.get("note"):
                item["note"] = e["note"]
            if e.get("terms"):
                item["terms"] = e["terms"]
            if e.get("tm_matches"):
                item["tm_matches"] = e["tm_matches"]
            review_entries.append(item)
        review["entries"] = review_entries

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(review, f, ensure_ascii=False, indent=2)

        existing = sum(1 for e in review_entries if e["translated"])
        print(f"📝 Batch {batch_num}/{state['total_batches']}  条目 {start + 1}-{end}（共 {total} 条）")
        print(f"   模式: 校对（跳过翻译）")
        print(f"   输出: {out_path.name}")
        print(f"   其中 {existing}/{len(review_entries)} 条已有译文")
        if context_entries:
            print(f"   上文: {len(context_entries)} 条")
        print(f"   校对后请将修正结果保存为 JSON，运行:")
        print(f"   python batch_translate/batch.py submit <reviewed.json>")
        return

    # ── 翻译模式 ──
    # 当前批次条目（去掉 target 字段，AI 只需要 source/terms/tm/context）
    batch_entries = []
    for e in entries[start:end]:
        item = {
            "id": e["id"],
            "source": e["source"],
        }
        if e.get("context"):
            item["context"] = e["context"]
        if e.get("note"):
            item["note"] = e["note"]
        if e.get("terms"):
            item["terms"] = e["terms"]
        if e.get("tm_matches"):
            item["tm_matches"] = e["tm_matches"]
        batch_entries.append(item)

    # 构建 batch JSON
    batch = {}
    batch["instructions"] = (
        "翻译过程中遇到任何不确定的术语、专有名词、角色名、上下文含义时，"
        "不要猜测，应主动搜索项目文件或联网搜索以获取准确信息后，再给出确定译文。"
        "最终返回结果必须是干净的译文，不要附加任何标注或说明。"
    )
    if state.get("document_summary"):
        batch["document_summary"] = state["document_summary"]
    if data.get("style_guide"):
        batch["style_guide"] = data["style_guide"]
    if context_entries:
        batch["previous"] = context_entries
    batch["batch"] = batch_num
    batch["total_batches"] = state["total_batches"]
    batch["entries"] = batch_entries

    out_path = _SCRIPT_DIR / "exports" / f"_batch_{batch_num:03d}_to_translate.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(batch, f, ensure_ascii=False, indent=2)

    print(f"📤 Batch {batch_num}/{state['total_batches']}  条目 {start + 1}-{end}（共 {total} 条）")
    print(f"   输出: {out_path.name}")
    if context_entries:
        print(f"   上文: {len(context_entries)} 条（id={context_entries[0]['id']}-{context_entries[-1]['id']}）")
    print(f"   翻译后请将结果保存为 JSON，运行:")
    print(f"   python batch_translate/batch.py submit <result.json>")


# ═══════════════════════════════════════════════════════════════════════
# submit
# ═══════════════════════════════════════════════════════════════════════

def cmd_submit(result_path: Path):
    """合并 AI 翻译结果，写回 mqxliff，推进到下一批。"""
    state = _load_state()

    if not result_path.is_file():
        print(f"❌ 结果文件不存在: {result_path}")
        sys.exit(1)

    # 读取 AI 结果
    with open(result_path, "r", encoding="utf-8") as f:
        results = json.load(f)

    if not isinstance(results, list):
        print("❌ 结果格式错误：应为 JSON 数组 [{id, target}, ...]")
        sys.exit(1)

    result_map = {str(r["id"]): r["target"] for r in results}
    print(f"📥 读取到 {len(result_map)} 条翻译")

    # 合并到 export JSON
    export_file = Path(state["export_file"])
    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    merged = 0
    for e in data["entries"]:
        if e["id"] in result_map:
            e["target"] = result_map[e["id"]]
            merged += 1

    with open(export_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"   已合并 {merged} 条 → {export_file.name}")

    # 写回源文件
    import subprocess
    work_file = Path(state["source_file"])
    tm_path = state.get("tm_path")

    if state["source_format"] == "mqxliff":
        # mqxliff: 用 mqxliff_tool.py import（含 TM 积累）
        import_args = [
            sys.executable, str(_SCRIPT_DIR / "mqxliff_tool.py"), "import",
            str(export_file),
            str(work_file),
            "--output", str(work_file),
        ]
        if tm_path:
            import_args += ["--save-tm", str(tm_path)]
        subprocess.run(import_args, check=True)
    else:
        # 其他格式: convert.py write
        write_args = [
            sys.executable, str(_SCRIPT_DIR / "convert.py"), "write",
            str(work_file),
            str(export_file),
            "--output", str(work_file),
        ]
        subprocess.run(write_args, check=True)
        # TM 积累：追加翻译到 tm_memory.json
        if tm_path:
            _accumulate_tm(export_file, tm_path)

    # 重新 parse（TM 已更新，获取最新 matches）
    reexport_file = _SCRIPT_DIR / "exports" / "_working.json"
    parse_args = [
        sys.executable, str(_SCRIPT_DIR / "convert.py"), "parse",
        str(work_file),
        "--output", str(reexport_file),
    ]
    subprocess.run(parse_args, check=True)

    # 对工作 JSON 做术语/TM/风格指南增强
    _enrich_working_json(reexport_file, state)

    # 推进状态
    state["current_batch"] += 1
    _save_state(state)

    # 检查是否全部完成
    if state["current_batch"] >= len(state["batches"]):
        print()
        print("🎉 全部翻译完成！")
        _STATE_FILE.unlink(missing_ok=True)
        return

    # 自动输出下一批
    print()
    cmd_next()


# ═══════════════════════════════════════════════════════════════════════
# review
# ═══════════════════════════════════════════════════════════════════════

def cmd_review(result_path: Path):
    """将翻译结果与原文合并，生成校对 JSON。"""
    state = _load_state()

    # 读取当前批的翻译任务 JSON
    batch_num = state["current_batch"] + 1
    batch_file = _SCRIPT_DIR / "exports" / f"_batch_{batch_num:03d}_to_translate.json"
    if not batch_file.is_file():
        print(f"❌ 找不到翻译任务文件: {batch_file.name}")
        sys.exit(1)
    with open(batch_file, "r", encoding="utf-8") as f:
        batch_data = json.load(f)

    # 读取翻译结果
    if not result_path.is_file():
        print(f"❌ 结果文件不存在: {result_path}")
        sys.exit(1)
    with open(result_path, "r", encoding="utf-8") as f:
        results = json.load(f)

    result_map = {str(r["id"]): r["target"] for r in results}

    # 构建校对 JSON
    review = {}
    review["instructions"] = (
        "逐条核对译文与原文：1)术语是否准确统一 2)标点格式是否符合规范 "
        "3)语气是否符合角色 4)表达是否自然流畅、无翻译腔。"
        "对不确定的术语或译法，主动搜索项目文件或联网验证后再修正。"
        "发现问题直接修正，无需标注。"
    )
    if state.get("document_summary"):
        review["document_summary"] = state["document_summary"]
    if batch_data.get("style_guide"):
        review["style_guide"] = batch_data["style_guide"]
    if batch_data.get("previous"):
        review["previous"] = batch_data["previous"]
    review["batch"] = batch_data["batch"]
    review["total_batches"] = batch_data["total_batches"]

    entries_with_translation = []
    for e in batch_data["entries"]:
        entry = dict(e)
        entry["translated"] = result_map.get(e["id"], "")
        entries_with_translation.append(entry)
    review["entries"] = entries_with_translation

    out_path = _SCRIPT_DIR / "exports" / f"_batch_{batch_num:03d}_to_review.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2)

    print(f"📝 校对文件已生成: {out_path.name}")
    print(f"   共 {len(entries_with_translation)} 条待校对")
    translated_count = sum(1 for e in entries_with_translation if e["translated"])
    print(f"   其中 {translated_count} 条已有译文")
    print(f"   校对后请将修正结果保存为 JSON，运行:")
    print(f"   python batch_translate/batch.py submit <reviewed.json>")


# ═══════════════════════════════════════════════════════════════════════
# status
# ═══════════════════════════════════════════════════════════════════════

def cmd_status():
    """显示当前进度。"""
    if not _STATE_FILE.is_file():
        print("未初始化。运行 init 开始。")
        return

    state = _load_state()
    batch_idx = state["current_batch"]
    if batch_idx < len(state["batches"]):
        s, e = state["batches"][batch_idx]
        print(f"进度: {e}/{state['total']} 条 ({batch_idx}/{state['total_batches']} 批)")
    else:
        print(f"进度: {state['total']}/{state['total']} 条（全部完成）")
    print(f"每批 ~{state['batch_chars']} 字, 上下文: {state['context_size']} 条")
    print(f"源文件: {state.get('source_file', state.get('mqxliff_file', 'unknown'))}")
    if state.get('tm_path'):
        tm = Path(state['tm_path'])
        if tm.is_file():
            with open(tm, encoding='utf-8') as f:
                tm_data = json.load(f)
            print(f"TM: {len(tm_data['entries'])} 条")


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="批量翻译工作流")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="初始化批量翻译")
    p_init.add_argument("file", type=str, help="源文件路径（mqxliff/docx/xlsx/txt...）")
    p_init.add_argument("--batch-chars", type=int, default=3000, help="每批字数阈值（默认 3000）")
    p_init.add_argument("--context-size", type=int, default=5, help="上文条数（默认 5）")
    p_init.add_argument("--terms", type=str, default=None, help="术语库 xlsx 路径")
    p_init.add_argument("--tm", type=str, default=None, help="翻译记忆 JSON 路径")
    p_init.add_argument("--style-guide", type=str, default=None, help="风格指南 txt 路径")
    p_init.add_argument("--source-col", type=str, default="A", help="xlsx 源列（默认 A）")
    p_init.add_argument("--target-col", type=str, default="B", help="xlsx 目标列（默认 B）")
    p_init.add_argument("--header-row", type=int, default=1, help="xlsx 表头行号（默认 1）")

    p_next = sub.add_parser("next", help="输出当前批翻译 JSON（--review 跳过翻译，直接校对）")
    p_next.add_argument("--review", action="store_true", help="跳过翻译，直接生成校对 JSON（用于已有译文的文件）")
    p_review = sub.add_parser("review", help="生成校对 JSON（翻译结果+原文对照）")
    p_review.add_argument("result", type=str, help="翻译结果 JSON 路径")
    p_submit = sub.add_parser("submit", help="提交校对结果并推进")
    p_submit.add_argument("result", type=str, help="校对后的结果 JSON 路径")
    p_status = sub.add_parser("status", help="查看进度")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(
            source_path=Path(args.file),
            batch_chars=args.batch_chars,
            context_size=args.context_size,
            terms_path=Path(args.terms) if args.terms else None,
            tm_path=Path(args.tm) if args.tm else None,
            style_guide_path=Path(args.style_guide) if args.style_guide else None,
            source_col=args.source_col,
            target_col=args.target_col,
            header_row=args.header_row,
        )
    elif args.command == "next":
        cmd_next(review_only=args.review)
    elif args.command == "review":
        cmd_review(Path(args.result))
    elif args.command == "submit":
        cmd_submit(Path(args.result))
    elif args.command == "status":
        cmd_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
