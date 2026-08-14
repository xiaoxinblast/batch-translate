#!/usr/bin/env python3
"""
批量翻译工作流：export → 分批发给 AI → 合并 → import → 循环
用法:
  python batch_translate/batch.py init <mqxliff> --batch-chars 3000 --context-size 5 ...
  python batch_translate/batch.py next
  python batch_translate/batch.py submit <result.json>
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

from validation import (
    effective_entry_policy,
    load_validation_policy,
    validate_batch_results,
)
from qa_checks import load_qa_policy, run_qa, validate_qa_report
from toolkit_version import TOOLKIT_VERSION, WORKFLOW_PROTOCOL_VERSION

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_SCRIPT_DIR = Path(__file__).resolve().parent
# _STATE_FILE 和 _DEFAULT_EXPORT 现在由 state 中的 stem 决定
# 保留这些作为 fallback（init 前尚未有 stem 时）
_DEFAULT_EXPORT = _SCRIPT_DIR / "exports" / "_working.json"
_ACTIVE_PROJECT = _SCRIPT_DIR / "data" / ".active_project"
_INVALID_PROJECT_ID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _validate_project_id(project_id: str) -> str:
    project_id = str(project_id).strip()
    if (
        not project_id
        or project_id in {".", ".."}
        or project_id.endswith((" ", "."))
        or _INVALID_PROJECT_ID.search(project_id)
    ):
        raise ValueError(f"无效的 project id: {project_id!r}")
    return project_id


def _project_state_path(project_id: str) -> Path:
    return _SCRIPT_DIR / "data" / project_id / "batch_state.json"


def _project_identity_path(project_id: str) -> Path:
    return _SCRIPT_DIR / "data" / project_id / "project_identity.json"


def _resolve_project_id(project_arg: Optional[str] = None) -> str:
    """返回显式 project id，或 data/.active_project 中的当前项目。"""
    if project_arg:
        try:
            return _validate_project_id(project_arg)
        except ValueError as exc:
            print(f"❌ {exc}")
            sys.exit(1)
    if _ACTIVE_PROJECT.is_file():
        project_id = _ACTIVE_PROJECT.read_text(encoding="utf-8").strip()
        if project_id:
            try:
                return _validate_project_id(project_id)
            except ValueError as exc:
                print(f"❌ {exc}")
                sys.exit(1)
    print("❌ 未指定 --project 且 data/.active_project 不可用")
    sys.exit(1)


def _get_state_path(project_arg: Optional[str] = None) -> Path:
    """返回指定项目或当前活动项目的 state 文件路径。"""
    if project_arg or _ACTIVE_PROJECT.is_file():
        return _project_state_path(_resolve_project_id(project_arg))
    # fallback: 旧格式（单文件平铺在 data/ 下）
    return _SCRIPT_DIR / "data" / "batch_state.json"


def _set_active_project(project_id: str):
    """设置当前活动的 project id。"""
    _ACTIVE_PROJECT.parent.mkdir(parents=True, exist_ok=True)
    _ACTIVE_PROJECT.write_text(_validate_project_id(project_id), encoding="utf-8")


def _set_active_stem(stem: str):
    """Backward-compatible alias for older callers."""
    _set_active_project(stem)


def _resolve_stem(stem_arg: Optional[str]) -> str:
    """Backward-compatible alias for project-id resolution."""
    return _resolve_project_id(stem_arg)


def _normalize_source_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _read_project_record(project_id: str) -> dict:
    paths = (
        _project_identity_path(project_id),
        _project_state_path(project_id),
        _SCRIPT_DIR / "exports" / project_id / "project_manifest.json",
    )
    for path in paths:
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            return record
    return {}


def _project_occupied(project_id: str) -> bool:
    for path in (
        _SCRIPT_DIR / "data" / project_id,
        _SCRIPT_DIR / "exports" / project_id,
    ):
        if path.is_file():
            return True
        if path.is_dir() and any(path.iterdir()):
            return True
    return False


def _record_matches_source(record: dict, source_path: Path) -> bool:
    recorded = record.get("input_source_file")
    if not recorded:
        return False
    return _normalize_source_path(recorded) == _normalize_source_path(source_path)


def _choose_project_id(
    source_path: Path,
    requested: Optional[str] = None,
    allow_legacy_resume: bool = False,
) -> str:
    """Choose a stable directory id without colliding on filename stem."""
    if requested:
        return _validate_project_id(requested)

    base = _validate_project_id(source_path.stem)
    if not _project_occupied(base):
        return base
    base_record = _read_project_record(base)
    if _record_matches_source(base_record, source_path):
        return base
    if (
        allow_legacy_resume
        and _project_state_path(base).is_file()
        and not base_record.get("input_source_file")
    ):
        return base

    digest = hashlib.sha256(
        _normalize_source_path(source_path).encode("utf-8")
    ).hexdigest()
    for length in (8, 12, 16, 64):
        candidate = _validate_project_id(f"{base}-{digest[:length]}")
        if not _project_occupied(candidate):
            return candidate
        if _record_matches_source(_read_project_record(candidate), source_path):
            return candidate
    raise ValueError(f"无法为源文件生成唯一 project id: {source_path}")


def _load_state(project_arg: Optional[str] = None) -> dict:
    state_path = _get_state_path(project_arg)
    if not state_path.is_file():
        print("❌ 未初始化，请先运行 init")
        sys.exit(1)
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict):
    project_id = state.get("project_id") or state.get("stem")
    state_path = _project_state_path(_validate_project_id(project_id))
    _write_json_atomic(state_path, state)


def _write_json_atomic(path: Path, data) -> None:
    """Write JSON beside its destination and atomically replace it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp_name).unlink(missing_ok=True)
        raise


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_rules_dir(project_id: str) -> Path:
    return _SCRIPT_DIR / "data" / project_id / "project_rules"


def _write_project_policy_snapshots(
    project_id: str,
    validation_policy: dict,
    qa_policy: dict,
    validation_source: Optional[Path],
    qa_source: Optional[Path],
) -> dict[str, str]:
    """Persist effective policies outside the Git-tracked toolkit code."""
    rules_dir = _project_rules_dir(project_id)
    rules_dir.mkdir(parents=True, exist_ok=True)
    validation_path = rules_dir / "validation_policy.json"
    qa_path = rules_dir / "qa_policy.json"
    manifest_path = rules_dir / "policy_manifest.json"
    _write_json_atomic(validation_path, validation_policy)
    _write_json_atomic(qa_path, qa_policy)
    manifest = {
        "schema_version": 1,
        "project_id": project_id,
        "workflow_protocol": WORKFLOW_PROTOCOL_VERSION,
        "sources": {
            "validation_policy": {
                "path": str(validation_source.resolve()) if validation_source else "built-in-default",
                "sha256": _sha256_file(validation_source) if validation_source else None,
            },
            "qa_policy": {
                "path": str(qa_source.resolve()) if qa_source else "built-in-default",
                "sha256": _sha256_file(qa_source) if qa_source else None,
            },
        },
        "approved": False,
        "approval_note": "默认策略快照；项目例外需经用户确认后更新",
    }
    _write_json_atomic(manifest_path, manifest)
    return {
        "validation_policy_path": str(validation_path.resolve()),
        "qa_policy_path": str(qa_path.resolve()),
        "policy_manifest_path": str(manifest_path.resolve()),
    }


# ═══════════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════════


def _build_parse_args(work_file: Path, export_file: Path, state: dict) -> list[str]:
    args = [
        sys.executable,
        str(_SCRIPT_DIR / "convert.py"),
        "parse",
        str(work_file),
        "--output",
        str(export_file),
    ]
    if work_file.suffix.lower() in (".xlsx", ".xlsm"):
        args += [
            "--source-col",
            state.get("source_col", "A"),
            "--target-col",
            state.get("target_col", "B"),
            "--header-row",
            str(state.get("header_row", 1)),
        ]
        if state.get("sheet_name"):
            args += ["--sheet", state["sheet_name"]]
    if work_file.suffix.lower() == ".mqxliff":
        args += ["--output-dir", str(export_file.parent)]
    return args


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
            "每条 entry 的 validation 是该条最终生效的校验规则，标签、长度、换行和空译文均以该对象为准。"
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
        if e.get("source_locked"):
            item["source_locked"] = True
            item["locked"] = True
        elif review_only:
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
        if isinstance(e.get("validation"), dict):
            item["validation"] = e["validation"]
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
    qa_policy_path: Optional[Path] = None,
    source_col: str = "A",
    target_col: str = "B",
    header_row: int = 1,
    sheet_name: Optional[str] = None,
    validation_policy_path: Optional[Path] = None,
    resume: bool = False,
    project_id: Optional[str] = None,
    force_reinit: bool = False,
) -> str:
    """初始化批量翻译：解析源文件 → 中间 JSON，写入 state。"""
    source_path = source_path.resolve()
    if not source_path.is_file():
        print(f"❌ 源文件不存在: {source_path}")
        sys.exit(1)
    try:
        validation_policy = load_validation_policy(validation_policy_path)
        qa_policy = load_qa_policy(qa_policy_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"❌ 项目策略无效: {exc}")
        sys.exit(1)

    try:
        project_id = _choose_project_id(
            source_path, project_id, allow_legacy_resume=resume
        )
    except ValueError as exc:
        print(f"❌ {exc}")
        sys.exit(1)
    stem = project_id
    state_path = _project_state_path(project_id)
    if state_path.is_file():
        if resume:
            _set_active_project(project_id)
            print("ℹ️ 状态文件已存在，无需重新初始化。直接运行 next 继续。")
            return project_id
        if not force_reinit:
            print(f"❌ 项目状态已存在，拒绝覆盖: {state_path}")
            print("   继续现有任务请运行 next；重新初始化请显式使用 --force-reinit")
            sys.exit(1)

    existing_record = _read_project_record(project_id)
    if (
        existing_record
        and not _record_matches_source(existing_record, source_path)
        and not (resume or force_reinit)
    ):
        print(f"❌ project id 已属于其他源文件: {project_id}")
        print("   请改用其他 --project，或确认后使用 --force-reinit")
        sys.exit(1)

    identity = {
        "project_id": project_id,
        "source_stem": source_path.stem,
        "original_source_name": source_path.name,
        "input_source_file": str(source_path.resolve()),
    }
    _write_json_atomic(_project_identity_path(project_id), identity)

    policy_snapshots = _write_project_policy_snapshots(
        project_id,
        validation_policy,
        qa_policy,
        validation_policy_path,
        qa_policy_path,
    )

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
        if sheet_name:
            parse_args += ["--sheet", sheet_name]
    if source_path.suffix.lower() == ".mqxliff":
        parse_args += ["--output-dir", str(export_dir)]

    import subprocess
    subprocess.run(parse_args, check=True)

    # 加载中间 JSON
    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    entries = data["entries"]
    total = len(entries)
    if total == 0:
        print("❌ 源文件中没有可翻译条目，未创建批次状态")
        sys.exit(1)

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
        "project_id": project_id,
        "stem": stem,
        "source_stem": source_path.stem,
        "original_source_name": source_path.name,
        "input_source_file": str(source_path.resolve()),
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
        "validation_policy_path": policy_snapshots["validation_policy_path"],
        "validation_policy": validation_policy,
        "qa_policy_path": policy_snapshots["qa_policy_path"],
        "qa_policy": qa_policy,
        "policy_manifest_path": policy_snapshots["policy_manifest_path"],
        "qa_required": True,
        "qa_status": "not_started",
        "source_col": source_col,
        "target_col": target_col,
        "header_row": header_row,
        "sheet_name": sheet_name,
    }
    _save_state(state)

    # 术语/TM 增强（即使 TM 为空也做术语匹配；跨文件 TM 可复用）
    _enrich_working_json(export_file, state)

    # 检测混合文件（部分条目有译文、部分无）
    with open(export_file, "r", encoding="utf-8") as f:
        enriched = json.load(f)
    parser_warnings = [
        str(message) for message in enriched.get("warnings", []) if message
    ]
    existing_targets = sum(1 for e in enriched["entries"] if e.get("target") and e["target"].strip())
    state["existing_targets"] = existing_targets
    state["parser_warnings"] = parser_warnings
    _save_state(state)
    _set_active_project(project_id)

    # 显示分批信息
    avg = sum(e - s for s, e in batches) / total_batches
    print(f"✅ 初始化完成")
    print(f"   Project: {project_id}")
    print(f"   文件: {export_file.name}")
    print(f"   总数: {total} 条, 每批 ~{batch_chars} 字, 共 {total_batches} 批（平均 ~{avg:.0f} 条/批）")
    print(f"   上下文窗口: {context_size} 条")
    for warning in parser_warnings:
        print(f"   ⚠️ 解析警告: {warning}")
    if 0 < existing_targets < total:
        print(f"   🔀 混合文件: {existing_targets}/{total} 条已有译文（translate 模式将自动锁定已有译文）")
    elif existing_targets == total:
        print(f"   📝 全部有译文: 建议使用 next --review 跳过翻译直接校对")
    print()
    print(document_summary)
    print()
    print("运行 next 获取第一批翻译任务。")
    if resume:
        print("ℹ️ --resume 模式：已有译文将自动锁定；若 exports/<project-id>/document_summary.md")
        print("   已存在，可跳过语境分析直接进入循环。")
    return project_id


# ═══════════════════════════════════════════════════════════════════════
# next
# ═══════════════════════════════════════════════════════════════════════

def cmd_next(review_only: bool = False, project_arg: Optional[str] = None):
    """输出当前批次的翻译 JSON（或校对 JSON，若 review_only=True）。"""
    state = _load_state(project_arg)

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

    try:
        validation_policy = state.get("validation_policy") or load_validation_policy(
            state.get("validation_policy_path")
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"❌ 验证策略无效: {exc}")
        sys.exit(1)

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
        review_source_entries = [
            {
                **entry,
                "validation": effective_entry_policy(
                    validation_policy, entry["id"]
                ),
            }
            for entry in entries[start:end]
        ]
        review = _build_review_json(
            review_source_entries,
            state,
            style_guide=data.get("style_guide", ""),
            previous=context_entries or None,
            batch_num=batch_num,
            review_only=True,
        )
        out_path = _SCRIPT_DIR / "exports" / state["stem"] / f"_batch_{batch_num:03d}_to_review.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(review, f, ensure_ascii=False, indent=2)

        state["qa_status"] = "awaiting_review"
        state["qa_batch"] = batch_num
        state["qa_input_sha256"] = None
        _save_state(state)

        existing = sum(1 for e in review["entries"] if e["translated"])
        print(f"📝 Batch {batch_num}/{state['total_batches']}  条目 {start + 1}-{end}（共 {total} 条）")
        print(f"   模式: 校对（跳过翻译）")
        print(f"   输出: {out_path.name}")
        print(f"   其中 {existing}/{len(review['entries'])} 条已有译文")
        if context_entries:
            print(f"   上文: {len(context_entries)} 条")
        reviewed_path = _current_reviewed_path(state)
        print(f"   校对后请将修正结果保存为: {reviewed_path}")
        print(f"   python batch_translate/batch.py qa --project {state['stem']}")
        return

    # ── 翻译模式 ──
    # 构建批次条目：已有译文的条目注入为 locked，空条目正常翻译
    batch_entries = []
    locked_count = 0
    existing_locked_count = 0
    source_locked_count = 0
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
        item["validation"] = effective_entry_policy(
            validation_policy, e["id"]
        )
        if e.get("maxlengthchars"):
            item["maxlengthchars"] = e["maxlengthchars"]
        if e.get("source_locked"):
            item["source_locked"] = True
        # 源文件锁定优先；否则混合文件中的已有译文继续按现有策略锁定。
        if e.get("source_locked"):
            item["target"] = existing_target
            item["locked"] = True
            item["note"] = (item.get("note", "") + " 【源文件锁定，严禁修改】").strip()
            locked_count += 1
            source_locked_count += 1
        elif existing_target:
            item["target"] = existing_target
            item["locked"] = True
            item["note"] = (item.get("note", "") + " 【已有100%匹配译文，严禁修改】").strip()
            locked_count += 1
            existing_locked_count += 1
        else:
            item["target"] = ""
            item["locked"] = False
        batch_entries.append(item)

    # 构建 batch JSON
    batch = {}
    if locked_count > 0:
        locked_details = []
        if existing_locked_count:
            locked_details.append(
                f"{existing_locked_count} 条已有译文（target 已填入）"
            )
        if source_locked_count:
            locked_details.append(
                f"{source_locked_count} 条源文件锁定条目（target 可能为空）"
            )
        instr = (
            f"本批共 {len(batch_entries)} 条，其中 {'；'.join(locked_details)}，"
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
        "每条 entry 的 validation 是该条最终生效的校验规则，标签、长度、换行和空译文均以该对象为准。"
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

    state["qa_status"] = "awaiting_translation"
    state["qa_batch"] = batch_num
    state["qa_input_sha256"] = None
    _save_state(state)

    print(f"📤 Batch {batch_num}/{state['total_batches']}  条目 {start + 1}-{end}（共 {total} 条）")
    if locked_count > 0:
        locked_details = []
        if existing_locked_count:
            locked_details.append(f"{existing_locked_count} 条已有译文")
        if source_locked_count:
            locked_details.append(f"{source_locked_count} 条源文件锁定")
        print(
            f"   🔒 其中 {'，'.join(locked_details)}（locked=true），"
            f"{len(batch_entries) - locked_count} 条待翻译"
        )
    print(f"   输出: {out_path.name}")
    if context_entries:
        print(f"   上文: {len(context_entries)} 条（id={context_entries[0]['id']}-{context_entries[-1]['id']}）")
    print(f"   翻译后请将结果保存为 JSON，运行:")
    print(f"   python batch_translate/batch.py review <translation.json> --project {state['stem']}")


# ═══════════════════════════════════════════════════════════════════════
# submit
# ═══════════════════════════════════════════════════════════════════════

def _load_expected_batch_entries(state: dict, export_data: dict) -> list[dict]:
    """Load the exact task contract, including locked targets and length limits."""
    batch_index = state["current_batch"]
    batch_num = batch_index + 1
    project_exports = _SCRIPT_DIR / "exports" / state["stem"]
    for suffix in ("to_review", "to_translate"):
        task_path = project_exports / f"_batch_{batch_num:03d}_{suffix}.json"
        if task_path.is_file():
            with open(task_path, "r", encoding="utf-8") as f:
                task = json.load(f)
            if isinstance(task.get("entries"), list):
                return task["entries"]

    start, end = state["batches"][batch_index]
    expected = []
    for source_entry in export_data["entries"][start:end]:
        entry = dict(source_entry)
        entry["locked"] = bool(source_entry.get("source_locked"))
        expected.append(entry)
    return expected


def _validate_submission(
    results,
    state: dict,
    allow_warnings: bool = False,
    warning_reason: Optional[str] = None,
) -> tuple[list[dict], list[str]]:
    """Run the same strict validation used by Step 4.5."""
    export_file = Path(state["export_file"])
    with open(export_file, "r", encoding="utf-8") as f:
        export_data = json.load(f)
    expected_entries = _load_expected_batch_entries(state, export_data)
    try:
        policy = state.get("validation_policy") or load_validation_policy(
            state.get("validation_policy_path")
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"❌ 验证策略无效: {exc}")
        sys.exit(1)

    report = validate_batch_results(results, expected_entries, policy)
    if report.fatal:
        print("❌ 提交校验失败，未写回、状态未推进:")
        for message in report.fatal:
            print(f"   - {message}")
        sys.exit(1)
    if report.warnings:
        for warning in report.warnings:
            print(f"⚠️ {warning}")
        if not allow_warnings:
            print("❌ 提交包含 warning；修正后重试，或显式使用 --allow-warnings --warning-reason <原因>")
            sys.exit(1)
        if not str(warning_reason or "").strip():
            print("❌ --allow-warnings 必须同时提供非空 --warning-reason")
            sys.exit(1)
    print(f"✅ 提交校验通过：{len(report.entries)} 条")
    return report.entries, report.warnings


def cmd_submit(
    result_path: Path,
    project_arg: Optional[str] = None,
    allow_warnings: bool = False,
    warning_reason: Optional[str] = None,
    _qa_approved: bool = False,
    _qa_record: Optional[dict] = None,
):
    """Validate and commit one batch as a recoverable transaction."""
    state = _load_state(project_arg)
    state_path = _project_state_path(state.get("project_id") or state["stem"])

    if (
        state.get("qa_required")
        and state.get("qa_status") != "clean"
        and not _qa_approved
    ):
        print("❌ 当前批次尚未完成 QA，不能直接 submit")
        print("   请先完成翻译→校对→QA；有 finding 时由 qa-reviewer 复核后使用 qa-submit")
        sys.exit(1)

    if not result_path.is_file():
        print(f"❌ 结果文件不存在: {result_path}")
        sys.exit(1)

    if state.get("qa_required") and state.get("qa_status") == "clean" and not _qa_approved:
        qa_baseline_hash = state.get("qa_input_sha256")
        if not qa_baseline_hash or _sha256_file(result_path) != qa_baseline_hash:
            print("❌ 提交文件不是 QA 通过的 reviewed 基线，请重新运行 QA")
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

    # 与 Step 4.5 共用同一校验器；返回已规范化的扁平数组。
    results, warnings = _validate_submission(
        results,
        state,
        allow_warnings=allow_warnings,
        warning_reason=warning_reason,
    )
    if warnings:
        state.setdefault("warning_acceptances", []).append({
            "batch": state["current_batch"] + 1,
            "reason": str(warning_reason).strip(),
            "warnings": list(warnings),
        })

    result_map = {str(r["id"]): r["target"] for r in results}
    print(f"📥 读取到 {len(result_map)} 条翻译")

    # 先在内存合并；批外 id 已由共享校验器拒绝。
    export_file = Path(state["export_file"])
    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    merged = 0
    for e in data["entries"]:
        if e["id"] in result_map:
            e["target"] = result_map[e["id"]]
            merged += 1

    import subprocess
    work_file = Path(state["source_file"])
    tm_path = state.get("tm_path")
    tm_file = Path(tm_path) if tm_path else None

    # 备份与候选文件放在工作文件同一卷，便于最终原子替换。
    with tempfile.TemporaryDirectory(
        prefix=f".submit_{state['stem']}_", dir=work_file.parent
    ) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        export_backup = temp_dir / "export.backup.json"
        work_backup = temp_dir / f"work.backup{work_file.suffix}"
        state_backup = temp_dir / "state.backup.json"
        shutil.copy2(export_file, export_backup)
        shutil.copy2(work_file, work_backup)
        shutil.copy2(state_path, state_backup)
        tm_existed = bool(tm_file and tm_file.is_file())
        tm_backup = temp_dir / "tm.backup.json"
        if tm_existed and tm_file:
            shutil.copy2(tm_file, tm_backup)
        manifest_path = (
            _SCRIPT_DIR / "exports" / state["stem"] / "project_manifest.json"
        )
        manifest_existed = manifest_path.is_file()
        manifest_backup = temp_dir / "manifest.backup.json"
        if manifest_existed:
            shutil.copy2(manifest_path, manifest_backup)

        merged_file = temp_dir / "merged.json"
        _write_json_atomic(merged_file, data)
        completed = False

        try:
            if state["source_format"] == "mqxliff":
                candidate_work = temp_dir / f"candidate{work_file.suffix}"
                submitted_file = temp_dir / "submitted.json"
                submitted_data = {
                    key: value for key, value in data.items() if key != "entries"
                }
                submitted_data["entries"] = [
                    entry for entry in data["entries"]
                    if entry["id"] in result_map
                ]
                _write_json_atomic(submitted_file, submitted_data)
                import_args = [
                    sys.executable,
                    str(_SCRIPT_DIR / "mqxliff_tool.py"),
                    "import",
                    str(submitted_file),
                    str(work_file),
                    "--output",
                    str(candidate_work),
                ]
                if tm_path:
                    import_args += ["--save-tm", str(tm_path)]
                subprocess.run(import_args, check=True)

                if state.get("existing_targets") == state["total"]:
                    committed_export = merged_file
                    print("ℹ️ review 模式：跳过重新解析与术语/TM 增强")
                else:
                    committed_export = temp_dir / "reparsed.json"
                    parse_args = _build_parse_args(
                        candidate_work, committed_export, state
                    )
                    subprocess.run(parse_args, check=True)
                    _enrich_working_json(committed_export, state)

                os.replace(candidate_work, work_file)
                os.replace(committed_export, export_file)
            else:
                # 单列/表格格式保持原始工作副本不变，最终 export 时一次写回。
                if tm_path:
                    _accumulate_tm(merged_file, tm_path)
                _enrich_working_json(merged_file, state)
                os.replace(merged_file, export_file)

            qa_status = state.get("qa_status")
            if state.get("qa_required") and qa_status in {"clean", "pending_agent"}:
                audit = {
                    "batch": state["current_batch"] + 1,
                    "status": "agent_reviewed" if _qa_record else "clean",
                    "finding_count": len((_qa_record or {}).get("findings", [])),
                }
                if _qa_record and _qa_record.get("_report_path"):
                    audit["report_path"] = _qa_record["_report_path"]
                state.setdefault("qa_history", []).append(audit)
            state["qa_status"] = "not_started"
            for key in (
                "qa_batch",
                "qa_input_sha256",
                "qa_task_path",
                "qa_reviewed_path",
                "qa_report_path",
                "qa_machine_findings",
            ):
                state.pop(key, None)
            state["current_batch"] += 1
            _save_state(state)
            completed = state["current_batch"] >= len(state["batches"])
            if completed:
                manifest = dict(state)
                manifest["completed"] = True
                _write_json_atomic(manifest_path, manifest)
                state_path.unlink()

        except Exception:
            rollback_errors = []
            for backup, destination in (
                (work_backup, work_file),
                (export_backup, export_file),
                (state_backup, state_path),
            ):
                try:
                    shutil.copy2(backup, destination)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{destination}: {rollback_exc}")
            if tm_file:
                try:
                    if tm_existed:
                        shutil.copy2(tm_backup, tm_file)
                    else:
                        tm_file.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{tm_file}: {rollback_exc}")
            try:
                if manifest_existed:
                    shutil.copy2(manifest_backup, manifest_path)
                else:
                    manifest_path.unlink(missing_ok=True)
            except Exception as rollback_exc:
                rollback_errors.append(f"{manifest_path}: {rollback_exc}")
            if rollback_errors:
                print("❌ 提交失败，且以下文件回滚失败:")
                for message in rollback_errors:
                    print(f"   - {message}")
            else:
                print("❌ 提交失败，工作文件、JSON 与 TM 已完整回滚，状态未推进。")
            raise

    print(f"   已提交 {merged} 条 → {export_file.name}")

    # 检查是否全部完成
    if completed:
        print()
        print("🎉 全部翻译完成！")
        return

    # 自动输出下一批（review 全译文模式继续走校对）
    print()
    cmd_next(
        review_only=(state.get("existing_targets") == state["total"]),
        project_arg=state.get("project_id") or state["stem"],
    )


# ═══════════════════════════════════════════════════════════════════════
# review
# ═══════════════════════════════════════════════════════════════════════

def cmd_review(result_path: Path, project_arg: Optional[str] = None):
    """将翻译结果与原文合并，生成校对 JSON。"""
    state = _load_state(project_arg)

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

    state["qa_status"] = "awaiting_review"
    state["qa_batch"] = batch_num
    state["qa_input_sha256"] = None
    _save_state(state)

    print(f"📝 校对文件已生成: {out_path.name}")
    print(f"   共 {len(merged)} 条待校对")
    translated_count = sum(1 for e in merged if e["translated"])
    print(f"   其中 {translated_count} 条已有译文")
    reviewed_path = _current_reviewed_path(state)
    print(f"   校对后请将修正结果保存为: {reviewed_path}")
    print(f"   python batch_translate/batch.py qa --project {state['stem']}")


# ═══════════════════════════════════════════════════════════════════════
# QA
# ═══════════════════════════════════════════════════════════════════════

def _current_reviewed_path(state: dict) -> Path:
    batch_num = state["current_batch"] + 1
    return _SCRIPT_DIR / "exports" / state["stem"] / f"_batch_{batch_num:03d}_reviewed.json"


def _current_qa_task_path(state: dict) -> Path:
    batch_num = state["current_batch"] + 1
    return _SCRIPT_DIR / "exports" / state["stem"] / f"_batch_{batch_num:03d}_qa_task.json"


def _current_qa_reviewed_path(state: dict) -> Path:
    batch_num = state["current_batch"] + 1
    return _SCRIPT_DIR / "exports" / state["stem"] / f"_batch_{batch_num:03d}_qa_reviewed.json"


def _current_qa_report_path(state: dict) -> Path:
    batch_num = state["current_batch"] + 1
    return _SCRIPT_DIR / "exports" / state["stem"] / f"_batch_{batch_num:03d}_qa_report.json"


def _load_json_file(path: Path, label: str) -> Any:
    if not path.is_file():
        print(f"❌ {label}不存在: {path}")
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        print(f"❌ {label} JSON 无效: {exc}")
        sys.exit(1)


def _current_batch_all_entries(state: dict, export_data: dict, results: list[dict]) -> list[dict]:
    """Merge current reviewed targets into the full working-entry snapshot."""
    result_map = {str(item["id"]): item["target"] for item in results}
    start, end = state["batches"][state["current_batch"]]
    all_entries = [dict(entry) for entry in export_data.get("entries", [])]
    for entry in all_entries[start:end]:
        if str(entry.get("id")) in result_map:
            entry["target"] = result_map[str(entry["id"])]
    return all_entries


def cmd_qa(project_arg: Optional[str] = None):
    """Run deterministic QA and prepare the QA-agent task for the current batch."""
    state = _load_state(project_arg)
    reviewed_path = _current_reviewed_path(state)
    if state.get("qa_status") not in {"awaiting_review", "pending_agent", "clean"}:
        print("❌ 当前批次尚未进入校对完成阶段，不能运行 QA")
        sys.exit(1)
    if state.get("qa_status") == "pending_agent" and _current_qa_task_path(state).is_file():
        if _sha256_file(reviewed_path) == state.get("qa_input_sha256"):
            print(f"ℹ️ 当前批次已有待处理 QA 任务: {_current_qa_task_path(state)}")
            return
        print("⚠️ reviewed 基线已变化，丢弃旧 QA 任务并重新运行 QA")

    raw_results = _load_json_file(reviewed_path, "reviewed 文件")
    results, warnings = _validate_submission(raw_results, state)
    if warnings:
        print("❌ reviewed 文件含 warning，QA 前必须先处理")
        sys.exit(1)

    export_data = _load_json_file(Path(state["export_file"]), "工作 JSON")
    expected_entries = _load_expected_batch_entries(state, export_data)
    try:
        policy = state.get("qa_policy") or load_qa_policy(state.get("qa_policy_path"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"❌ QA 策略无效: {exc}")
        sys.exit(1)

    all_entries = _current_batch_all_entries(state, export_data, results)
    qa_result = run_qa(results, expected_entries, policy, all_entries=all_entries)
    batch_num = state["current_batch"] + 1
    result_map = {str(item["id"]): item["target"] for item in results}
    findings_by_id: dict[str, list[dict]] = {}
    for finding in qa_result["findings"]:
        findings_by_id.setdefault(str(finding["id"]), []).append(finding)

    source_task = _SCRIPT_DIR / "exports" / state["stem"] / f"_batch_{batch_num:03d}_to_review.json"
    source_task_data = _load_json_file(source_task, "校对任务") if source_task.is_file() else {}
    qa_reviewed_path = _current_qa_reviewed_path(state)
    qa_report_path = _current_qa_report_path(state)
    task_entries = []
    for entry in expected_entries:
        item = dict(entry)
        entry_id = str(item.get("id"))
        item["target"] = result_map.get(entry_id, item.get("target", item.get("translated", "")))
        item["findings"] = findings_by_id.get(entry_id, [])
        item.pop("translated", None)
        task_entries.append(item)
    task = {
        "schema_version": 1,
        "mode": "qa",
        "project": state["stem"],
        "batch": batch_num,
        "total_batches": state["total_batches"],
        "instructions": (
            "逐条查看 findings。确认是真问题时修正 target 并标记 fixed；"
            "确认是误报时原样保留 target 并标记 false_positive。"
            "必须处理全部 finding，严禁修改 locked 或 source_locked 条目。"
        ),
        "document_summary": source_task_data.get("document_summary", state.get("document_summary", "")),
        "style_guide": source_task_data.get("style_guide", export_data.get("style_guide", "")),
        "previous": source_task_data.get("previous"),
        "policy": policy,
        "qa_reviewed_path": str(qa_reviewed_path.resolve()),
        "qa_report_path": str(qa_report_path.resolve()),
        "entries": task_entries,
        "findings": qa_result["findings"],
        "summary": qa_result["summary"],
    }
    task_path = _current_qa_task_path(state)
    _write_json_atomic(task_path, task)
    state["qa_batch"] = batch_num
    state["qa_input_sha256"] = _sha256_file(reviewed_path)
    state["qa_task_path"] = str(task_path.resolve())
    state["qa_reviewed_path"] = str(qa_reviewed_path.resolve())
    state["qa_report_path"] = str(qa_report_path.resolve())
    state["qa_machine_findings"] = qa_result["findings"]
    state["qa_status"] = "clean" if not qa_result["findings"] else "pending_agent"
    _save_state(state)
    print(f"✅ QA 完成：{len(qa_result['findings'])} 个候选问题")
    print(f"   任务: {task_path}")
    if not qa_result["findings"]:
        print("   未发现候选问题，可直接提交 reviewed JSON")
    else:
        print(f"   请启动 qa-reviewer，写入: {qa_reviewed_path}")
        print(f"   QA 报告写入: {qa_report_path}")


def cmd_qa_submit(
    result_path: Path,
    report_path: Path,
    project_arg: Optional[str] = None,
):
    """Validate QA-agent decisions, then commit the corrected batch transactionally."""
    state = _load_state(project_arg)
    if state.get("qa_status") != "pending_agent":
        print("❌ 当前批次没有待处理的 QA 任务")
        sys.exit(1)
    reviewed_path = _current_reviewed_path(state)
    if _sha256_file(reviewed_path) != state.get("qa_input_sha256"):
        print("❌ reviewed 基线在 QA 期间发生变化，请重新运行 QA")
        sys.exit(1)

    task = _load_json_file(_current_qa_task_path(state), "QA 任务")
    if not isinstance(task, dict):
        print("❌ QA 任务必须是 JSON 对象")
        sys.exit(1)
    expected_result_raw = task.get("qa_reviewed_path")
    expected_report_raw = task.get("qa_report_path")
    expected_result_path = (
        Path(expected_result_raw) if isinstance(expected_result_raw, str) else None
    )
    expected_report_path = (
        Path(expected_report_raw) if isinstance(expected_report_raw, str) else None
    )
    canonical_result_path = _current_qa_reviewed_path(state).resolve()
    canonical_report_path = _current_qa_report_path(state).resolve()
    if (
        expected_result_path is None
        or expected_report_path is None
        or not expected_result_path.is_absolute()
        or not expected_report_path.is_absolute()
        or os.path.normcase(str(expected_result_path.resolve()))
        != os.path.normcase(str(canonical_result_path))
        or os.path.normcase(str(expected_report_path.resolve()))
        != os.path.normcase(str(canonical_report_path))
        or os.path.normcase(str(result_path.resolve()))
        != os.path.normcase(str(expected_result_path.resolve()))
        or os.path.normcase(str(report_path.resolve()))
        != os.path.normcase(str(expected_report_path.resolve()))
    ):
        print("❌ QA 输出文件路径与 QA task 不一致，请按 task 中的绝对路径写入")
        sys.exit(1)
    report = _load_json_file(report_path, "QA 报告")
    raw_baseline = _load_json_file(reviewed_path, "reviewed 文件")
    baseline_results, baseline_warnings = _validate_submission(raw_baseline, state)
    if baseline_warnings:
        print("❌ reviewed 基线含 warning，不能提交 QA 结果")
        sys.exit(1)
    raw_results = _load_json_file(result_path, "QA reviewed 文件")
    qa_results, qa_warnings = _validate_submission(raw_results, state)
    if qa_warnings:
        print("❌ QA reviewed 文件含 warning，不能提交")
        sys.exit(1)

    export_data = _load_json_file(Path(state["export_file"]), "工作 JSON")
    expected_entries = _load_expected_batch_entries(state, export_data)
    try:
        policy = state.get("qa_policy") or load_qa_policy(state.get("qa_policy_path"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"❌ QA 策略无效: {exc}")
        sys.exit(1)
    report_errors = validate_qa_report(
        report,
        task.get("findings", []),
        baseline_results,
        qa_results,
    )
    all_entries = _current_batch_all_entries(state, export_data, qa_results)
    recheck = run_qa(qa_results, expected_entries, policy, all_entries=all_entries)
    false_positive_ids = {
        str(item.get("finding_id"))
        for item in report.get("findings", [])
        if isinstance(item, dict) and item.get("status") == "false_positive"
    }
    remaining = [
        finding["finding_id"]
        for finding in recheck["findings"]
        if finding["finding_id"] not in false_positive_ids
    ]
    if remaining:
        report_errors.append(f"QA 修正后仍有未处理 finding: {sorted(remaining)}")
    if report_errors:
        print("❌ QA 报告校验失败:")
        for error in report_errors:
            print(f"   - {error}")
        sys.exit(1)

    cmd_submit(
        result_path,
        project_arg,
        _qa_approved=True,
        _qa_record={
            **report,
            "_report_path": str(report_path.resolve()),
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# status
# ═══════════════════════════════════════════════════════════════════════

def cmd_status(project_arg: Optional[str] = None):
    """显示当前进度。"""
    state_path = _get_state_path(project_arg)
    if not state_path.is_file():
        print("未初始化。运行 init 开始。")
        return

    state = _load_state(project_arg)
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

def cmd_retry(project_arg: Optional[str] = None):
    """重新生成当前批次的翻译 JSON（用于 Agent 输出格式错误后重试）。"""
    state = _load_state(project_arg)
    if state["current_batch"] >= len(state["batches"]):
        print("✅ 全部已完成，无需重试。")
        return
    print("🔄 重新生成当前批次...")
    cmd_next(project_arg=state.get("project_id") or state["stem"])


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
            "source_col": state.get("source_col", "A"),
            "target_col": state.get("target_col", "B"),
            "header_row": state.get("header_row", 1),
            "sheet_name": state.get("sheet_name"),
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
            "source_col": "A",
            "target_col": "B",
            "header_row": 1,
            "sheet_name": None,
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
    parse_args = _build_parse_args(work_file, export_file, enrich_state)
    subprocess.run(parse_args, check=True)

    _enrich_working_json(export_file, enrich_state)
    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"✅ refresh 完成：{len(data.get('entries', []))} 条已重新注入术语/TM/风格指南 → {export_file}")


def cmd_export(stem_arg: Optional[str], out_arg: Optional[str], force: bool):
    """Export the accumulated translations to the source format."""
    import subprocess

    stem = _resolve_stem(stem_arg)
    project_dir = _SCRIPT_DIR / "exports" / stem
    export_file = project_dir / "_working.json"
    if not export_file.is_file():
        print(f"❌ 工作 JSON 不存在: {export_file}")
        sys.exit(1)

    state_path = _SCRIPT_DIR / "data" / stem / "batch_state.json"
    manifest_path = project_dir / "project_manifest.json"
    record = {}
    for metadata_path in (state_path, manifest_path):
        if metadata_path.is_file():
            with open(metadata_path, "r", encoding="utf-8") as f:
                record = json.load(f)
            break
    if record.get("current_batch", 0) < record.get("total_batches", 0):
        print("❌ 项目尚未完成全部批次，拒绝导出不完整文件")
        sys.exit(1)

    if record.get("source_file"):
        work_file = Path(record["source_file"])
    else:
        candidates = sorted((_SCRIPT_DIR / "data" / stem).glob(f"_working_{stem}.*"))
        work_file = candidates[0] if candidates else Path()
    if not work_file.is_file():
        print(f"❌ 工作源文件不存在: {work_file}")
        sys.exit(1)

    if out_arg:
        dst = Path(out_arg)
    else:
        output_name = record.get("original_source_name") or f"{stem}{work_file.suffix}"
        dst = _SCRIPT_DIR.parent / "已交付" / output_name
    if dst.is_file() and not force:
        print(f"❌ 目标已存在（加 --force 覆盖）: {dst}")
        sys.exit(1)

    protected_paths = {work_file.resolve()}
    if record.get("input_source_file"):
        protected_paths.add(Path(record["input_source_file"]).resolve())
    if dst.resolve() in protected_paths:
        print("❌ 导出目标不得覆盖用户源文件或受管工作副本，请指定新文件")
        sys.exit(1)

    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".export_", dir=dst.parent) as temp_name:
        temp_dir = Path(temp_name)
        candidate = temp_dir / f"candidate{work_file.suffix}"
        if work_file.suffix.lower() == ".mqxliff":
            subprocess.run([
                sys.executable, str(_SCRIPT_DIR / "mqxliff_tool.py"), "import",
                str(export_file), str(work_file), "--output", str(candidate),
            ], check=True)
            try:
                sys.path.insert(0, str(_SCRIPT_DIR))
                from mqxliff_tool import parse_mqxliff
                units, _ = parse_mqxliff(candidate)
                policy = record.get("validation_policy") or load_validation_policy(
                    record.get("validation_policy_path")
                )
                empty_ids = [
                    unit.id for unit in units
                    if not unit.is_locked
                    and not (unit.target_text or "").strip()
                    and not effective_entry_policy(policy, unit.id)["allow_empty"]
                ]
                if empty_ids:
                    raise ValueError(
                        f"有 {len(empty_ids)}/{len(units)} 条可翻译单元为空: "
                        f"{empty_ids[:20]}"
                    )
                print(f"  ✅ 导出校验通过：{len(units)} 条 trans-unit")
            except Exception as exc:
                print(f"❌ 导出校验失败（文件可能损坏）: {exc}")
                sys.exit(1)
        else:
            subprocess.run([
                sys.executable, str(_SCRIPT_DIR / "convert.py"), "write",
                str(work_file), str(export_file), "--output", str(candidate),
            ], check=True)
            parsed_file = temp_dir / "parsed.json"
            subprocess.run(
                _build_parse_args(candidate, parsed_file, record), check=True
            )
            with open(export_file, "r", encoding="utf-8") as f:
                expected_data = json.load(f)
            with open(parsed_file, "r", encoding="utf-8") as f:
                parsed_data = json.load(f)
            expected = {
                str(entry["id"]): entry.get("target", "")
                for entry in expected_data.get("entries", [])
                if entry.get("target", "")
            }
            parsed = {str(entry["id"]): entry for entry in parsed_data.get("entries", [])}
            strip_tags = re.compile(r"<tag\b[^<>]*/>")
            mismatched = []
            for entry_id, target in expected.items():
                actual_entry = parsed.get(entry_id, {})
                if work_file.suffix.lower() in (".xlsx", ".xlsm"):
                    actual = actual_entry.get("target", "")
                else:
                    actual = actual_entry.get("source", "")
                if strip_tags.sub("", actual) != strip_tags.sub("", target):
                    mismatched.append(entry_id)
            if mismatched:
                print(f"❌ 导出校验失败，{len(mismatched)} 条译文不一致: {mismatched[:20]}")
                sys.exit(1)
            print(f"  ✅ 导出校验通过：{len(expected)} 条译文")

        os.replace(candidate, dst)

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


def _split_context_entries(entries: list[dict], max_chars: int) -> list[list[dict]]:
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")
    parts = []
    current = []
    current_chars = 0
    for entry in entries:
        entry_chars = len(str(entry.get("source", "")))
        if current and current_chars + entry_chars > max_chars:
            parts.append(current)
            current = []
            current_chars = 0
        current.append(entry)
        current_chars += entry_chars
    if current:
        parts.append(current)
    return parts


def cmd_context_split(max_chars: int, project_arg: Optional[str] = None) -> Path:
    """Create complete-entry context-analysis parts and a manifest."""
    state = _load_state(project_arg)
    with open(state["export_file"], "r", encoding="utf-8") as f:
        data = json.load(f)
    try:
        parts = _split_context_entries(data.get("entries", []), max_chars)
    except ValueError as exc:
        print(f"❌ {exc}")
        sys.exit(1)
    if not parts:
        print("❌ 工作 JSON 中没有可分析条目")
        sys.exit(1)

    output_dir = _SCRIPT_DIR / "exports" / state["stem"] / "context_parts"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_parts = []
    for index, entries in enumerate(parts, 1):
        payload = {
            "mode": "context_part",
            "source_file": data.get("source_file", state.get("original_source_name")),
            "part": index,
            "total_parts": len(parts),
            "instructions": (
                "仅分析本分片并写出分片报告；保留所有重要 id 关联。"
                "不得把分片结论冒充全局结论。"
            ),
            "source_warnings": data.get("warnings", []),
            "entries": entries,
        }
        part_path = output_dir / f"context_part_{index:03d}.json"
        _write_json_atomic(part_path, payload)
        manifest_parts.append({
            "part": index,
            "path": str(part_path.resolve()),
            "entries": len(entries),
            "first_id": str(entries[0]["id"]),
            "last_id": str(entries[-1]["id"]),
        })
    manifest = {
        "project": state.get("project_id") or state["stem"],
        "source_file": data.get("source_file", state.get("original_source_name")),
        "max_chars": max_chars,
        "total_parts": len(parts),
        "parts": manifest_parts,
    }
    manifest_path = output_dir / "context_manifest.json"
    _write_json_atomic(manifest_path, manifest)
    print(f"✅ 语境分析已分为 {len(parts)} 片: {manifest_path}")
    return manifest_path


def cmd_context_pack(
    report_paths: list[Path],
    out_arg: Optional[str],
    project_arg: Optional[str] = None,
) -> Path:
    """Pack ordered part reports into a final synthesis task JSON."""
    project_id = _resolve_project_id(project_arg)
    reports = []
    for index, path in enumerate(report_paths, 1):
        path = path.resolve()
        if not path.is_file():
            print(f"❌ 分片报告不存在: {path}")
            sys.exit(1)
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            print(f"❌ 分片报告为空: {path}")
            sys.exit(1)
        reports.append({"part": index, "path": str(path), "report": content})

    if out_arg:
        output = Path(out_arg)
    else:
        output = (
            _SCRIPT_DIR / "exports" / project_id / "context_parts"
            / "context_merge_task.json"
        )
    payload = {
        "mode": "context_merge",
        "project": project_id,
        "instructions": (
            "综合全部分片报告为一份全局语境报告。去重并解决冲突，"
            "补充跨分片关联；最终报告必须覆盖规定的全部章节。"
        ),
        "part_reports": reports,
    }
    _write_json_atomic(output, payload)
    print(f"✅ 全局语境合并任务已生成: {output}")
    return output


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
        "--sheet", type=str, default=None,
        help="xlsx 工作表名称；传 * 处理全部工作表（默认活动表）",
    )
    p_init.add_argument(
        "--validation-policy", type=str, default=None,
        help="项目验证策略 JSON 路径",
    )
    p_init.add_argument(
        "--qa-policy", type=str, default=None,
        help="项目 QA 策略 JSON 路径",
    )
    p_init.add_argument(
        "--resume",
        action="store_true",
        help="从带已有译文的 mqxliff 恢复初始化（状态已存在时不覆盖，直接 next 继续）",
    )
    p_init.add_argument(
        "--project", type=str, default=None,
        help="显式 project id（默认使用文件 stem；同名冲突时自动加路径哈希）",
    )
    p_init.add_argument(
        "--force-reinit", action="store_true",
        help="明确覆盖该 project 的现有初始化状态",
    )

    def add_project_argument(command_parser):
        command_parser.add_argument(
            "--project", "--stem", dest="project", type=str, default=None,
            help="project id（默认使用 data/.active_project）",
        )

    p_next = sub.add_parser("next", help="输出当前批翻译 JSON（--review 跳过翻译，直接校对）")
    p_next.add_argument("--review", action="store_true", help="跳过翻译，直接生成校对 JSON（用于已有译文的文件）")
    add_project_argument(p_next)
    p_review = sub.add_parser("review", help="生成校对 JSON（翻译结果+原文对照）")
    p_review.add_argument("result", type=str, help="翻译结果 JSON 路径")
    add_project_argument(p_review)
    p_qa = sub.add_parser("qa", help="运行程序化 QA 并生成 QA 代理任务")
    add_project_argument(p_qa)
    p_qa_submit = sub.add_parser("qa-submit", help="提交 QA 代理修正结果并推进")
    p_qa_submit.add_argument("result", type=str, help="QA 修正后的完整 JSON 路径")
    p_qa_submit.add_argument("--report", required=True, type=str, help="QA 判定报告 JSON 路径")
    add_project_argument(p_qa_submit)
    p_submit = sub.add_parser("submit", help="提交校对结果并推进")
    p_submit.add_argument("result", type=str, help="校对后的结果 JSON 路径")
    add_project_argument(p_submit)
    p_submit.add_argument(
        "--allow-warnings", action="store_true",
        help="人工确认当前 warning 可接受后放行",
    )
    p_submit.add_argument(
        "--warning-reason", type=str, default=None,
        help="放行 warning 的具体理由（与 --allow-warnings 同时使用）",
    )
    p_status = sub.add_parser("status", help="查看进度")
    add_project_argument(p_status)
    p_retry = sub.add_parser("retry", help="重新生成当前批次翻译 JSON")
    add_project_argument(p_retry)
    p_summary = sub.add_parser("summary", help="写入语境分析报告到 document_summary")
    p_summary.add_argument("report", type=str, help="报告文件路径（UTF-8 文本）")
    add_project_argument(p_summary)
    p_refresh = sub.add_parser("refresh", help="重新解析工作文件并重跑术语/TM/风格指南增强")
    add_project_argument(p_refresh)
    p_refresh.add_argument("--tm", type=str, default=None,
                           help="TM JSON 路径（state 不存在时默认 data/tm_memory.json）")
    p_refresh.add_argument("--terms", type=str, default=None,
                           help="术语库 xlsx 路径（state 不存在时默认 data/term_base.xlsx）")
    p_refresh.add_argument("--style-guide", type=str, default=None,
                           help="风格指南 txt 路径（state 不存在时默认 data/style_guide.txt）")
    p_export = sub.add_parser("export", help="导出最终译文 mqxliff")
    add_project_argument(p_export)
    p_export.add_argument("--out", type=str, default=None,
                          help="输出路径（默认 已交付/<stem>.mqxliff）")
    p_export.add_argument("--force", action="store_true", help="覆盖已存在的目标文件")
    p_gaps = sub.add_parser("term-gaps", help="生成术语缺口待确认清单")
    add_project_argument(p_gaps)
    p_gaps.add_argument("--out", type=str, default=None,
                        help="输出路径（默认 _temp/term_gaps_<stem>.md）")
    p_version = sub.add_parser("version", help="显示工具包与工作流协议版本")
    p_version.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_context_split = sub.add_parser("context-split", help="生成语境分析分片")
    p_context_split.add_argument(
        "--max-chars", type=int, default=60000,
        help="每片 source 字符上限（默认 60000，不拆分单条 entry）",
    )
    add_project_argument(p_context_split)
    p_context_pack = sub.add_parser("context-pack", help="生成分片报告合并任务")
    p_context_pack.add_argument("reports", nargs="+", type=Path, help="按顺序排列的分片报告")
    p_context_pack.add_argument("--out", type=str, default=None, help="合并任务 JSON 路径")
    add_project_argument(p_context_pack)

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(
            source_path=Path(args.file),
            batch_chars=args.batch_chars,
            context_size=args.context_size,
            terms_path=Path(args.terms) if args.terms else None,
            tm_path=Path(args.tm) if args.tm else None,
            style_guide_path=Path(args.style_guide) if args.style_guide else None,
            qa_policy_path=Path(args.qa_policy) if args.qa_policy else None,
            source_col=args.source_col,
            target_col=args.target_col,
            header_row=args.header_row,
            sheet_name=args.sheet,
            validation_policy_path=(
                Path(args.validation_policy) if args.validation_policy else None
            ),
            resume=args.resume,
            project_id=args.project,
            force_reinit=args.force_reinit,
        )
    elif args.command == "next":
        cmd_next(review_only=args.review, project_arg=args.project)
    elif args.command == "review":
        cmd_review(Path(args.result), args.project)
    elif args.command == "qa":
        cmd_qa(args.project)
    elif args.command == "qa-submit":
        cmd_qa_submit(Path(args.result), Path(args.report), args.project)
    elif args.command == "submit":
        cmd_submit(
            Path(args.result),
            args.project,
            allow_warnings=args.allow_warnings,
            warning_reason=args.warning_reason,
        )
    elif args.command == "status":
        cmd_status(args.project)
    elif args.command == "retry":
        cmd_retry(args.project)
    elif args.command == "summary":
        cmd_summary(Path(args.report), args.project)
    elif args.command == "refresh":
        cmd_refresh(args.project, args.tm, args.terms, args.style_guide)
    elif args.command == "export":
        cmd_export(args.project, args.out, args.force)
    elif args.command == "term-gaps":
        cmd_term_gaps(args.project, args.out)
    elif args.command == "version":
        version_info = {
            "toolkit_version": TOOLKIT_VERSION,
            "workflow_protocol": WORKFLOW_PROTOCOL_VERSION,
            "python_requires": ">=3.10",
        }
        if args.json:
            print(json.dumps(version_info, ensure_ascii=False))
        else:
            print(
                f"batch-translate {TOOLKIT_VERSION} "
                f"(workflow protocol {WORKFLOW_PROTOCOL_VERSION}, Python >=3.10)"
            )
    elif args.command == "context-split":
        cmd_context_split(args.max_chars, args.project)
    elif args.command == "context-pack":
        cmd_context_pack(args.reports, args.out, args.project)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
