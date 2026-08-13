#!/usr/bin/env python3
"""
批量翻译工作流：export → 分批发给 AI → 合并 → import → 循环
用法:
  python batch_translate/batch.py init <mqxliff> --batch-chars 3000 --context-size 5 ...
  python batch_translate/batch.py next
  python batch_translate/batch.py submit <result.json>
"""

import argparse
import json
import re
import shutil
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


def _resolve_stem(stem_arg: Optional[str]) -> str:
    """返回 --stem 参数或 .active_project 中的 stem。"""
    if stem_arg:
        return stem_arg
    if _ACTIVE_PROJECT.is_file():
        stem = _ACTIVE_PROJECT.read_text(encoding="utf-8").strip()
        if stem:
            return stem
    print("❌ 未指定 --stem 且 data/.active_project 不可用")
    sys.exit(1)


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
        # 提交后的译文是已确认版本，同键旧译文必须被覆盖
        tm.add(entries, replace=True)
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
            sys.path.insert(0, str(_SCRIPT_DIR))
            from term_base import TermBase
            tb = TermBase(terms_path)
            tb.load()
            import re
            tr = re.compile(r"<[^>]+>")
            entries = data.get("entries", [])
            term_failures = 0
            for e in entries:
                try:
                    plain = tr.sub("", e.get("source", ""))
                    terms = tb.find_terms(plain)
                    if terms:
                        e["terms"] = terms
                except Exception as exc:
                    term_failures += 1
                    if term_failures <= 3:
                        print(f"  ⚠️ 术语匹配失败 id={e.get('id')}: {exc}")
            if term_failures:
                print(f"⚠️ 术语增强完成，{term_failures}/{len(entries)} 条失败（已跳过）")
        except ImportError as e:
            print(f"⚠️ term_base 导入失败，跳过术语增强: {e}")
        except Exception as e:
            print(f"⚠️ 术语增强过程出错，跳过: {e}")

    # TM
    tm_path = state.get("tm_path")
    if tm_path:
        try:
            sys.path.insert(0, str(_SCRIPT_DIR))
            from tm_store import TranslationMemory, display_tags
            tm = TranslationMemory(tm_path)
            import re
            tr = re.compile(r"<[^>]+>")
            entries = data.get("entries", [])
            tm_failures = 0
            for e in entries:
                try:
                    plain = tr.sub("", e.get("source", ""))
                    matches = tm.find_matches(plain, query_context=e.get("context", ""))
                    if matches:
                        e["tm_matches"] = [
                            {**m, "source": display_tags(m["source"]), "target": display_tags(m["target"])}
                            for m in matches
                        ]
                    # 片段匹配
                    if not matches or all(m["similarity"] < 0.85 for m in matches):
                        exclude = {m["source"] for m in matches} if matches else None
                        frag_matches = tm.find_fragment_matches(plain, exclude_sources=exclude)
                        if frag_matches:
                            e["tm_fragments"] = [
                                {**f, "match_source": display_tags(f["match_source"]),
                                 "match_target": display_tags(f["match_target"])}
                                for f in frag_matches
                            ]
                except Exception as exc:
                    tm_failures += 1
                    if tm_failures <= 3:
                        print(f"  ⚠️ TM 匹配失败 id={e.get('id')}: {exc}")
            if tm_failures:
                print(f"⚠️ TM 增强完成，{tm_failures}/{len(entries)} 条失败（已跳过）")
        except ImportError as e:
            print(f"⚠️ tm_store 导入失败，跳过 TM 增强: {e}")
        except Exception as e:
            print(f"⚠️ TM 增强过程出错，跳过: {e}")

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
            "每条 entry 可能带有 tm_matches（翻译记忆参考）、tm_fragments（片段匹配参考）和 terms（术语约束），核对时参考。"
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
        translated = e.get("translated", e.get("target", ""))
        item = {
            "id": e["id"],
            "source": e["source"],
            "translated": translated,
        }
        if review_only:
            # review 模式：已有译文是待校对对象，不标记为锁定
            item["locked"] = False
        elif "locked" in e:
            item["locked"] = bool(e["locked"])
        else:
            item["locked"] = bool(str(translated).strip())
        if e.get("context"):
            item["context"] = e["context"]
        if e.get("note"):
            item["note"] = e["note"]
        if e.get("maxlengthchars"):
            item["maxlengthchars"] = e["maxlengthchars"]
        if e.get("terms"):
            item["terms"] = e["terms"]
        if e.get("tm_matches"):
            item["tm_matches"] = e["tm_matches"]
        if e.get("tm_fragments"):
            item["tm_fragments"] = e["tm_fragments"]
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
    resume: bool = False,
):
    """初始化批量翻译：解析源文件 → 中间 JSON，写入 state。"""
    stem = source_path.stem  # 不含扩展名的文件名，用作目录名
    _set_active_stem(stem)

    state_path = _get_state_path()
    if state_path.is_file():
        if resume:
            print("ℹ️ 状态文件已存在，无需重新初始化。直接运行 next 继续。")
            return
        print("⚠️ 状态文件已存在，将覆盖。")
        print("  如需继续之前的任务，请直接运行 next")

    # 复制源文件到工作文件（不动源文件）
    import shutil
    work_dir = _SCRIPT_DIR / "data" / stem
    work_dir.mkdir(parents=True, exist_ok=True)
    if resume:
        # resume 时归一为规范工作文件名，避免从 _working_*.mqxliff 再次加前缀
        work_file = work_dir / f"_working_{stem}{source_path.suffix.lower()}"
    else:
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

    # 检测混合文件（部分条目有译文、部分无）
    with open(export_file, "r", encoding="utf-8") as f:
        enriched = json.load(f)
    existing_targets = sum(1 for e in enriched["entries"] if e.get("target") and e["target"].strip())
    state["existing_targets"] = existing_targets
    _save_state(state)

    # 显示分批信息
    avg = sum(e - s for s, e in batches) / total_batches
    print(f"✅ 初始化完成")
    print(f"   文件: {export_file.name}")
    print(f"   总数: {total} 条, 每批 ~{batch_chars} 字, 共 {total_batches} 批（平均 ~{avg:.0f} 条/批）")
    print(f"   上下文窗口: {context_size} 条")
    if 0 < existing_targets < total:
        print(f"   🔀 混合文件: {existing_targets}/{total} 条已有译文（translate 模式将自动锁定已有译文）")
    elif existing_targets == total:
        print(f"   📝 全部有译文: 建议使用 next --review 跳过翻译直接校对")
    print()
    print(document_summary)
    print()
    print("运行 next 获取第一批翻译任务。")
    if resume:
        print("ℹ️ --resume 模式：已有译文将自动锁定；若 exports/<stem>/document_summary.md")
        print("   已存在，可跳过语境分析直接进入循环。")


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
    # 构建批次条目：已有译文的条目注入为 locked，空条目正常翻译
    batch_entries = []
    locked_count = 0
    for e in entries[start:end]:
        existing_target = e.get("target", "").strip()
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
        if e.get("tm_fragments"):
            item["tm_fragments"] = e["tm_fragments"]
        # 混合文件支持：已有译文 → 锁定，空条目 → 待翻译
        if existing_target:
            item["target"] = existing_target
            item["locked"] = True
            item["note"] = (item.get("note", "") + " 【已有100%匹配译文，严禁修改】").strip()
            locked_count += 1
        else:
            item["target"] = ""
            item["locked"] = False
        batch_entries.append(item)

    # 构建 batch JSON
    batch = {}
    if locked_count > 0:
        instr = (
            f"本批共 {len(batch_entries)} 条，其中 {locked_count} 条 locked=true（已有100%匹配译文，target 已填入），"
            f"请直接保留其 target，严禁对译文进行任何改动。"
            f"其余 {len(batch_entries) - locked_count} 条 target 为空，需要从零翻译。"
        )
    else:
        instr = ""
    batch["instructions"] = (
        "翻译过程中遇到任何不确定的术语、专有名词、角色名、上下文含义时，"
        "不要猜测，应主动搜索项目文件或联网搜索以获取准确信息后，再给出确定译文。"
        "每条 entry 可能带有 tm_matches（翻译记忆模糊匹配，高相似度可直接复用）"
        "和 terms（术语库匹配），翻译时优先参考。"
        "原文中的 <tag .../> 内联标签必须原样保留在译文中，数量和位置不变。"
        "最终返回结果必须是干净的译文，不要附加任何标注或说明。"
        + ("\n\n" + instr if instr else "")
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
    if locked_count > 0:
        print(f"   🔒 其中 {locked_count} 条已有译文（locked=true），{len(batch_entries) - locked_count} 条待翻译")
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

        # review 全译文模式：target 已由校对确认，跳过重新 parse 与术语/TM 增强
        # （避免每批提交都全量重算 36k 术语 × 全部条目的匹配，造成数分钟等待）
        if state.get("existing_targets") == state["total"]:
            print("ℹ️ review 模式：跳过重新解析与术语/TM 增强")
        else:
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

    # 自动输出下一批（review 全译文模式继续走校对）
    print()
    cmd_next(review_only=(state.get("existing_targets") == state["total"]))


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


def cmd_summary(report_file: Path, stem_arg: Optional[str]):
    """把语境分析报告写入 batch_state.json 的 document_summary，并保留 sidecar。"""
    if not report_file.is_file():
        print(f"❌ 报告文件不存在: {report_file}")
        sys.exit(1)
    stem = _resolve_stem(stem_arg)
    text = report_file.read_text(encoding="utf-8")

    state_path = _SCRIPT_DIR / "data" / stem / "batch_state.json"
    if state_path.is_file():
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        state["document_summary"] = text
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f"✅ document_summary 已写入: {state_path}")
    else:
        print(f"⚠️ 状态文件不存在（未 init 或已完成清理），仅写 sidecar: {state_path}")

    sidecar = _SCRIPT_DIR / "exports" / stem / "document_summary.md"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(text, encoding="utf-8")
    print(f"📄 sidecar 已写入: {sidecar}")


def cmd_refresh(
    stem_arg: Optional[str],
    tm_arg: Optional[str] = None,
    terms_arg: Optional[str] = None,
    style_guide_arg: Optional[str] = None,
):
    """重新解析工作文件并重跑术语/TM/风格指南增强。

    适用于 init 时增强中断/不完整，或 TM/术语库更新后刷新参考。
    有 state 时复用 state 记录的路径；state 已清理（全部提交完成）时
    默认使用 batch_translate/data/ 下的编译产物，可用 --tm/--terms/--style-guide 覆盖。
    注意：以工作 mqxliff 为事实源重新 parse，若手工只改过 _working.json
    而未写回 mqxliff，这些改动不会保留。
    """
    import subprocess
    stem = _resolve_stem(stem_arg)
    state_path = _SCRIPT_DIR / "data" / stem / "batch_state.json"
    if state_path.is_file():
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        work_file = Path(state["source_file"])
        enrich_state = {
            "terms_path": state.get("terms_path"),
            "tm_path": state.get("tm_path"),
            "style_guide_path": state.get("style_guide_path"),
        }
    else:
        work_file = _SCRIPT_DIR / "data" / stem / f"_working_{stem}.mqxliff"
        enrich_state = {
            "terms_path": str(Path(terms_arg).resolve()) if terms_arg
                         else str(_SCRIPT_DIR / "data" / "term_base.xlsx"),
            "tm_path": str(Path(tm_arg).resolve()) if tm_arg
                       else str(_SCRIPT_DIR / "data" / "tm_memory.json"),
            "style_guide_path": str(Path(style_guide_arg).resolve()) if style_guide_arg
                                else str(_SCRIPT_DIR / "data" / "style_guide.txt"),
        }

    if not work_file.is_file():
        print(f"❌ 工作文件不存在: {work_file}")
        sys.exit(1)
    for key, label in (("terms_path", "术语库"), ("tm_path", "TM"),
                       ("style_guide_path", "风格指南")):
        p = enrich_state.get(key)
        if p and not Path(p).is_file():
            print(f"⚠️ {label}不存在: {p}（该部分将跳过）")

    export_file = _SCRIPT_DIR / "exports" / stem / "_working.json"
    export_file.parent.mkdir(parents=True, exist_ok=True)
    parse_args = [
        sys.executable, str(_SCRIPT_DIR / "convert.py"), "parse",
        str(work_file), "--output", str(export_file),
    ]
    if work_file.suffix.lower() == ".mqxliff":
        parse_args += ["--output-dir", str(export_file.parent)]
    subprocess.run(parse_args, check=True)

    _enrich_working_json(export_file, enrich_state)
    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"✅ refresh 完成：{len(data.get('entries', []))} 条已重新注入术语/TM/风格指南 → {export_file}")


def cmd_export(stem_arg: Optional[str], out_arg: Optional[str], force: bool):
    """导出最终译文 mqxliff（默认复制到项目 已交付/ 目录）。"""
    stem = _resolve_stem(stem_arg)
    export_file = _SCRIPT_DIR / "exports" / stem / "_working.json"
    if not export_file.is_file():
        print(f"❌ 工作 JSON 不存在: {export_file}")
        sys.exit(1)
    work_file = _SCRIPT_DIR / "data" / stem / f"_working_{stem}.mqxliff"
    if not work_file.is_file():
        print(f"❌ 工作 mqxliff 不存在: {work_file}")
        sys.exit(1)

    if out_arg:
        dst = Path(out_arg)
    else:
        dst = _SCRIPT_DIR.parent / "已交付" / f"{stem}.mqxliff"
    if dst.is_file() and not force:
        print(f"❌ 目标已存在（加 --force 覆盖）: {dst}")
        sys.exit(1)

    dst.parent.mkdir(parents=True, exist_ok=True)
    # 始终以最新 _working.json 为事实源重新写回，避免直接修改 JSON 后导出旧数据
    import subprocess
    subprocess.run([
        sys.executable, str(_SCRIPT_DIR / "mqxliff_tool.py"), "import",
        str(export_file), str(work_file), "--output", str(dst),
    ], check=True)

    # 导出后校验：重新解析目标文件，确认 XML 合法且全部条目有译文
    try:
        sys.path.insert(0, str(_SCRIPT_DIR))
        from mqxliff_tool import parse_mqxliff
        units, _ = parse_mqxliff(dst)
        total = len(units)
        empty = sum(1 for u in units if not (u.target_text or "").strip())
        if empty:
            print(f"❌ 导出校验失败：{dst} 有 {empty}/{total} 条空译文")
            sys.exit(1)
        print(f"  ✅ 导出校验通过：{total} 条 trans-unit，全部有译文")
    except SystemExit:
        raise
    except Exception as e:
        print(f"❌ 导出校验失败（文件可能损坏）: {e}")
        sys.exit(1)

    print(f"✅ 已导出: {dst}")


def cmd_term_gaps(stem_arg: Optional[str], out_arg: Optional[str]):
    """从 document_summary 提取“疑似术语库未覆盖的专名”小节，生成待确认清单。"""
    stem = _resolve_stem(stem_arg)
    sidecar = _SCRIPT_DIR / "exports" / stem / "document_summary.md"
    if sidecar.is_file():
        text = sidecar.read_text(encoding="utf-8")
    else:
        batch_file = _SCRIPT_DIR / "exports" / stem / "_batch_001_to_translate.json"
        if not batch_file.is_file():
            print(f"❌ 找不到 document_summary：{sidecar} 与 {batch_file} 均不存在")
            sys.exit(1)
        with open(batch_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        text = data.get("document_summary") or ""
        if not text:
            print(f"❌ {batch_file} 中没有 document_summary")
            sys.exit(1)

    marker = "疑似术语库未覆盖的专名"
    start = text.find(marker)
    if start == -1:
        section = "(未找到术语缺口小节)"
    else:
        start = text.rfind("\n", 0, start) + 1  # 从该小节的标题行行首开始
        rest = text[start:]
        end = len(rest)
        for sep in ("\n## ", "\n==== "):
            i = rest.find(sep)
            if i != -1:
                end = min(end, i)
        section = rest[:end].strip()

    if out_arg:
        out = Path(out_arg)
    else:
        out = _SCRIPT_DIR.parent / "_temp" / f"term_gaps_{stem}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# 术语缺口待确认清单（{stem}）\n\n{section}\n", encoding="utf-8")
    print(f"✅ 术语缺口清单已生成: {out}")


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
    p_init.add_argument(
        "--resume",
        action="store_true",
        help="从带已有译文的 mqxliff 恢复初始化（状态已存在时不覆盖，直接 next 继续）",
    )

    p_next = sub.add_parser("next", help="输出当前批翻译 JSON（--review 跳过翻译，直接校对）")
    p_next.add_argument("--review", action="store_true", help="跳过翻译，直接生成校对 JSON（用于已有译文的文件）")
    p_review = sub.add_parser("review", help="生成校对 JSON（翻译结果+原文对照）")
    p_review.add_argument("result", type=str, help="翻译结果 JSON 路径")
    p_submit = sub.add_parser("submit", help="提交校对结果并推进")
    p_submit.add_argument("result", type=str, help="校对后的结果 JSON 路径")
    p_status = sub.add_parser("status", help="查看进度")
    p_retry = sub.add_parser("retry", help="重新生成当前批次翻译 JSON")
    p_summary = sub.add_parser("summary", help="写入语境分析报告到 document_summary")
    p_summary.add_argument("report", type=str, help="报告文件路径（UTF-8 文本）")
    p_summary.add_argument("--stem", type=str, default=None,
                           help="项目 stem（默认 data/.active_project）")
    p_refresh = sub.add_parser("refresh", help="重新解析工作文件并重跑术语/TM/风格指南增强")
    p_refresh.add_argument("--stem", type=str, default=None,
                           help="项目 stem（默认 data/.active_project）")
    p_refresh.add_argument("--tm", type=str, default=None,
                           help="TM JSON 路径（state 不存在时默认 data/tm_memory.json）")
    p_refresh.add_argument("--terms", type=str, default=None,
                           help="术语库 xlsx 路径（state 不存在时默认 data/term_base.xlsx）")
    p_refresh.add_argument("--style-guide", type=str, default=None,
                           help="风格指南 txt 路径（state 不存在时默认 data/style_guide.txt）")
    p_export = sub.add_parser("export", help="导出最终译文 mqxliff")
    p_export.add_argument("--stem", type=str, default=None,
                          help="项目 stem（默认 data/.active_project）")
    p_export.add_argument("--out", type=str, default=None,
                          help="输出路径（默认 已交付/<stem>.mqxliff）")
    p_export.add_argument("--force", action="store_true", help="覆盖已存在的目标文件")
    p_gaps = sub.add_parser("term-gaps", help="生成术语缺口待确认清单")
    p_gaps.add_argument("--stem", type=str, default=None,
                        help="项目 stem（默认 data/.active_project）")
    p_gaps.add_argument("--out", type=str, default=None,
                        help="输出路径（默认 _temp/term_gaps_<stem>.md）")

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
            resume=args.resume,
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
    elif args.command == "summary":
        cmd_summary(Path(args.report), args.stem)
    elif args.command == "refresh":
        cmd_refresh(args.stem, args.tm, args.terms, args.style_guide)
    elif args.command == "export":
        cmd_export(args.stem, args.out, args.force)
    elif args.command == "term-gaps":
        cmd_term_gaps(args.stem, args.out)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
