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
# _STATE_FILE 和 _DEFAULT_EXPORT 现在由 state 中的 stem 决定
# 保留这些作为 fallback（init 前尚未有 stem 时）
_DEFAULT_EXPORT = _SCRIPT_DIR / "exports" / "_working.json"
_ACTIVE_PROJECT = _SCRIPT_DIR / "data" / ".active_project"


def _get_state_path() -> Path:
    """从 .active_project 读取当前 stem，返回 state 文件路径。"""
    if _ACTIVE_PROJECT.is_file():
        stem = _ACTIVE_PROJECT.read_text(encoding="utf-8").strip()
        return _SCRIPT_DIR / "data" / stem / "batch_state.json"
    # fallback: 旧格式（单文件平铺在 data/ 下）
    return _SCRIPT_DIR / "data" / "batch_state.json"


def _set_active_stem(stem: str):
    """设置当前活动的项目 stem。"""
    _ACTIVE_PROJECT.parent.mkdir(parents=True, exist_ok=True)
    _ACTIVE_PROJECT.write_text(stem, encoding="utf-8")


def _load_state() -> dict:
    state_path = _get_state_path()
    if not state_path.is_file():
        print("❌ 未初始化，请先运行 init")
        sys.exit(1)
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict):
    state_path = _get_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
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

    # terms（所有格式统一处理，包括 mqxliff；init 时 TM 为空，
    # submit 后重导出也不带 TM，因此必须在此补做）
    terms_path = state.get("terms_path")
    if terms_path:
        try:
            from term_base import TermBase
            tb = TermBase(terms_path)
            tb.load()
            import re
            tr = re.compile(r"<[^>]+>")
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
            tr = re.compile(r"<[^>]+>")
            for e in data.get("entries", []):
                plain = tr.sub("", e.get("source", ""))
                matches = tm.find_matches(plain, query_context=e.get("context", ""))
                if matches:
                    e["tm_matches"] = matches
                # 片段匹配
                if not matches or all(m["similarity"] < 0.85 for m in matches):
                    frag_matches = tm.find_fragment_matches(plain)
                    if frag_matches:
                        e["tm_fragments"] = frag_matches
        except ImportError:
            pass

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _generate_summary(entries: list, batches: list, batch_chars: int) -> str:
    """生成极简文档摘要。真正的语境分析由 SKILL 步骤 5 的 Agent 完成。"""
    import re
    _tag_re = re.compile(r"<[^>]+>")

    total = len(entries)
    total_chars = sum(len(_tag_re.sub("", e["source"])) for e in entries)
    has_tags = sum(1 for e in entries if "<tag" in e["source"])
    has_target = sum(1 for e in entries if e.get("target", "").strip())

    return (
        f"总条目: {total}  纯文本字数: {total_chars}  批次: {len(batches)}（每批 ~{batch_chars} 字）\n"
        f"内联标签: {has_tags} 条  已有译文: {has_target} 条"
    )


def _build_review_json(
    entries: list[dict],
    state: dict,
    *,
    style_guide: str = "",
    previous: list[dict] | None = None,
    batch_num: int = 1,
    review_only: bool = False,
) -> dict:
    """构建校对 JSON。entries 每条需含 id/source/translated。"""
    review: dict = {}
    review["instructions"] = (
        "逐条核对译文与原文：1)术语是否准确统一 2)标点格式是否符合规范 "
        "3)语气是否符合角色 4)表达是否自然流畅、无翻译腔。"
        + (
            "每条 entry 可能带有 tm_matches（翻译记忆参考）和 terms（术语约束），核对时参考。"
            "内联标签（<tag .../>）必须原样保留，数量与位置与 source 一致——丢失标签是最严重的错误。"
        )
        + ("发现问题直接修正，无需标注。" if review_only else "")
    )
    if state.get("document_summary"):
        review["document_summary"] = state["document_summary"]
    if style_guide:
        review["style_guide"] = style_guide
    if previous:
        review["previous"] = previous
    review["batch"] = batch_num
    review["total_batches"] = state["total_batches"]

    review_entries = []
    for e in entries:
        item = {
            "id": e["id"],
            "source": e["source"],
            "translated": e.get("translated", e.get("target", "")),
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
    return review


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
    stem = source_path.stem  # 不含扩展名的文件名，用作目录名
    _set_active_stem(stem)

    state_path = _get_state_path()
    if state_path.is_file():
        print("⚠️ 状态文件已存在，将覆盖。")
        print("  如需继续之前的任务，请直接运行 next")

    # 复制源文件到工作文件（不动源文件）
    import shutil
    work_dir = _SCRIPT_DIR / "data" / stem
    work_dir.mkdir(parents=True, exist_ok=True)
    work_file = work_dir / f"_working_{source_path.name}"
    shutil.copy2(source_path, work_file)

    # 用 convert.py 解析
    export_dir = _SCRIPT_DIR / "exports" / stem
    export_dir.mkdir(parents=True, exist_ok=True)
    export_file = export_dir / "_working.json"
    parse_args = [
        sys.executable, str(_SCRIPT_DIR / "convert.py"), "parse",
        str(work_file),
        "--output", str(export_file),
    ]
    if source_path.suffix.lower() in (".xlsx", ".xlsm"):
        parse_args += ["--source-col", source_col, "--target-col", target_col,
                       "--header-row", str(header_row)]
    if source_path.suffix.lower() == ".mqxliff":
        parse_args += ["--output-dir", str(export_dir)]

    import subprocess
    subprocess.run(parse_args, check=True)

    # 加载中间 JSON
    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    entries = data["entries"]
    total = len(entries)

    # 按字数分批
    import re
    _tag_re = re.compile(r"<[^>]+>")
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
        "stem": stem,
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

    # 术语/TM 增强（即使 TM 为空也做术语匹配；跨文件 TM 可复用）
    _enrich_working_json(export_file, state)

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
        review = _build_review_json(
            entries[start:end],
            state,
            style_guide=data.get("style_guide", ""),
            previous=context_entries or None,
            batch_num=batch_num,
            review_only=True,
        )
        out_path = _SCRIPT_DIR / "exports" / state["stem"] / f"_batch_{batch_num:03d}_to_review.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(review, f, ensure_ascii=False, indent=2)

        existing = sum(1 for e in review["entries"] if e["translated"])
        print(f"📝 Batch {batch_num}/{state['total_batches']}  条目 {start + 1}-{end}（共 {total} 条）")
        print(f"   模式: 校对（跳过翻译）")
        print(f"   输出: {out_path.name}")
        print(f"   其中 {existing}/{len(review['entries'])} 条已有译文")
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
        "每条 entry 可能带有 tm_matches（翻译记忆模糊匹配，高相似度可直接复用）"
        "和 terms（术语库匹配），翻译时优先参考。"
        "原文中的 <tag .../> 内联标签必须原样保留在译文中，数量和位置不变。"
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

    out_path = _SCRIPT_DIR / "exports" / state["stem"] / f"_batch_{batch_num:03d}_to_translate.json"
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

def _validate_submission(results: list, state: dict) -> None:
    """提交前分级校验：致命错误退出（不写回、不推进），非致命仅警告。

    致命：缺 id/target 字段、重复 id、本批预期 id 未全覆盖。
    警告：内联标签数与 source 不一致、target 为空但 source 有可译文本、
          提交了不属于本批的 id。
    """
    import re
    from collections import Counter

    export_file = Path(state["export_file"])
    with open(export_file, "r", encoding="utf-8") as f:
        export_data = json.load(f)
    start, end = state["batches"][state["current_batch"]]
    batch_entries = export_data["entries"][start:end]
    expected_ids = {str(e["id"]) for e in batch_entries}
    source_by_id = {str(e["id"]): e.get("source", "") for e in batch_entries}

    # ── 致命：字段完整性 ──
    for i, r in enumerate(results):
        if not isinstance(r, dict) or "id" not in r:
            print(f"❌ 校验失败：第 {i} 条缺少 'id' 字段，未写回、状态未推进")
            sys.exit(1)
        if "target" not in r:
            print(f"❌ 校验失败：id={r.get('id')} 缺少 'target' 字段，未写回、状态未推进")
            sys.exit(1)

    submitted_ids = [str(r["id"]) for r in results]

    # ── 致命：重复 id ──
    dupes = [i for i, c in Counter(submitted_ids).items() if c > 1]
    if dupes:
        print(f"❌ 校验失败：结果含重复 id（{len(dupes)} 个）：{sorted(dupes)[:20]}")
        print("   → 未写回、状态未推进，可修正后重新 submit。")
        sys.exit(1)

    # ── 致命：本批预期 id 未全覆盖 ──
    submitted_set = set(submitted_ids)
    missing = expected_ids - submitted_set
    if missing:
        print(f"❌ 校验失败：缺少本批 {len(missing)}/{len(expected_ids)} 条译文"
              f"（提交 {len(submitted_set)} 条，可能只提交了改动条）")
        print(f"   缺失 id（前 20）：{sorted(missing)[:20]}")
        print("   → 未写回、状态未推进，请补全全部条目后重新 submit。")
        sys.exit(1)

    # ── 警告：不属于本批的 id ──
    extra = submitted_set - expected_ids
    if extra:
        print(f"⚠️ 警告：{len(extra)} 条 id 不属于本批（将按 id 匹配写到对应条目，"
              f"请确认无误）：{sorted(extra)[:20]}")

    # ── 警告：标签数 / 空 target ──
    TAG_RE = re.compile(r"<tag\s+id=['\"][^'\"]+['\"].*?/>")
    STRIP_TAG = re.compile(r"<[^>]+>")
    tag_warn, empty_warn = [], []
    for r in results:
        rid = str(r["id"])
        target = r.get("target") or ""
        source = source_by_id.get(rid, "")
        if "<tag" in source and len(TAG_RE.findall(source)) != len(TAG_RE.findall(target)):
            tag_warn.append(rid)
        if not target.strip() and STRIP_TAG.sub("", source).strip():
            empty_warn.append(rid)
    if tag_warn:
        print(f"⚠️ 警告：{len(tag_warn)} 条内联标签数与 source 不一致：{tag_warn[:20]}")
    if empty_warn:
        print(f"⚠️ 警告：{len(empty_warn)} 条 target 为空但 source 含可译文本：{empty_warn[:20]}")

    tail = "（含警告，见上）" if (extra or tag_warn or empty_warn) else ""
    print(f"✅ 提交校验通过：{len(results)} 条，本批 id 全覆盖{tail}")


def cmd_submit(result_path: Path):
    """合并 AI 翻译结果，写回 mqxliff，推进到下一批。"""
    state = _load_state()
    state_path = _get_state_path()

    if not result_path.is_file():
        print(f"❌ 结果文件不存在: {result_path}")
        sys.exit(1)

    # 读取 AI 结果
    with open(result_path, "r", encoding="utf-8") as f:
        try:
            results = json.load(f)
        except json.JSONDecodeError as e:
            print(f"❌ JSON 解析失败: {e}")
            # 诊断常见问题
            import re
            f.seek(0)
            content = f.read()
            # 检测中文语境下的 ASCII 引号（U+0022 冒充中文引号）
            suspicious = re.findall(r'[一-鿿　-〿]"[一-鿿]', content)
            if suspicious:
                print(f"   ⚠️ 发现 {len(suspicious)} 处中文引号误用 ASCII 引号 (U+0022)")
                print(f"   示例: {suspicious[:3]}")
                print(f"   修复方法: 将中文语境下的 \" 替换为弯引号 “ / ”")
            # 检测字面 tab 字符
            if '\t' in content:
                lines_with_tab = [i+1 for i, line in enumerate(content.split('\n')) if '\t' in line]
                if len(lines_with_tab) <= 5:
                    print(f"   ⚠️ 文件中含字面 tab 字符，行号: {lines_with_tab}")
            sys.exit(1)

    if not isinstance(results, list):
        print("❌ 结果格式错误：应为 JSON 数组 [{id, target}, ...]")
        sys.exit(1)

    # 提交前分级校验（致命错误退出、不写回、不推进）
    _validate_submission(results, state)

    result_map = {str(r["id"]): r["target"] for r in results}
    print(f"📥 读取到 {len(result_map)} 条翻译")

    # 合并到 export JSON（先备份，失败时恢复）
    export_file = Path(state["export_file"])
    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    backup_data = json.dumps(data, ensure_ascii=False)  # 回滚用

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

    try:
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
        reexport_file = _SCRIPT_DIR / "exports" / state["stem"] / "_working.json"
        parse_args = [
            sys.executable, str(_SCRIPT_DIR / "convert.py"), "parse",
            str(work_file),
            "--output", str(reexport_file),
        ]
        subprocess.run(parse_args, check=True)

        # 对工作 JSON 做术语/TM/风格指南增强
        _enrich_working_json(reexport_file, state)

    except Exception:
        # 回滚：恢复 _working.json，状态不变
        with open(export_file, "w", encoding="utf-8") as f:
            f.write(backup_data)
        print("❌ 提交失败，已回滚 _working.json，状态未推进，可安全重试。")
        raise

    # 推进状态
    state["current_batch"] += 1
    _save_state(state)

    # 检查是否全部完成
    if state["current_batch"] >= len(state["batches"]):
        print()
        print("🎉 全部翻译完成！")
        state_path.unlink(missing_ok=True)
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
    batch_file = _SCRIPT_DIR / "exports" / state["stem"] / f"_batch_{batch_num:03d}_to_translate.json"
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
    merged = []
    for e in batch_data["entries"]:
        entry = dict(e)
        entry["translated"] = result_map.get(e["id"], "")
        merged.append(entry)

    review = _build_review_json(
        merged,
        state,
        style_guide=batch_data.get("style_guide", ""),
        previous=batch_data.get("previous"),
        batch_num=batch_data["batch"],
        review_only=False,
    )

    out_path = _SCRIPT_DIR / "exports" / state["stem"] / f"_batch_{batch_num:03d}_to_review.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2)

    print(f"📝 校对文件已生成: {out_path.name}")
    print(f"   共 {len(merged)} 条待校对")
    translated_count = sum(1 for e in merged if e["translated"])
    print(f"   其中 {translated_count} 条已有译文")
    print(f"   校对后请将修正结果保存为 JSON，运行:")
    print(f"   python batch_translate/batch.py submit <reviewed.json>")


# ═══════════════════════════════════════════════════════════════════════
# status
# ═══════════════════════════════════════════════════════════════════════

def cmd_status():
    """显示当前进度。"""
    state_path = _get_state_path()
    if not state_path.is_file():
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
# retry
# ═══════════════════════════════════════════════════════════════════════

def cmd_retry():
    """重新生成当前批次的翻译 JSON（用于 Agent 输出格式错误后重试）。"""
    state = _load_state()
    if state["current_batch"] >= len(state["batches"]):
        print("✅ 全部已完成，无需重试。")
        return
    print("🔄 重新生成当前批次...")
    cmd_next()


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
    p_retry = sub.add_parser("retry", help="重新生成当前批次翻译 JSON")

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
    elif args.command == "retry":
        cmd_retry()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
