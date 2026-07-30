#!/usr/bin/env python3
"""用 translate 工作流的 parse 管线从已翻译批次重建 TM。

流程：
  1. mqxliff/docx/txt → convert.py parse → JSON
     xlsx/xlsm → openpyxl 直接读（parser 不提取 target 列）
  2. 从 entries 提取 source/target → TranslationMemory.add()
  3. 保存 TM
"""
import sys, json, subprocess, tempfile
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
from tm_store import TranslationMemory

XLSX_EXTS = {".xlsx", ".xlsm"}
PIPELINE_EXTS = {".mqxliff", ".docx", ".txt", ".csv", ".tsv"}


# ── xlsx/xlsm 直接提取 ────────────────────────────────────────────

def _col_idx(letter: str) -> int:
    """A→0, B→1, ..."""
    r = 0
    for c in letter.upper():
        r = r * 26 + (ord(c) - ord("A") + 1)
    return r - 1

def _cell(row: tuple, idx: int) -> str:
    if idx >= len(row) or row[idx] is None:
        return ""
    return str(row[idx]).strip()

def _detect_columns(ws, source_col: str, target_col: str) -> tuple[int, int, int]:
    """检测源/译文列索引和标题行号。若用户未指定，扫描前 5 行自动识别。
    返回 (src_idx, tgt_idx, header_row)。header_row=0 表示未检测到标题行。"""
    import re

    if source_col and target_col:
        return _col_idx(source_col), _col_idx(target_col), 0

    src_keywords = ["原文", "source", "ja", "jp", "日文", "日本語"]
    tgt_keywords = ["译文", "target", "zh", "sc", "cn", "中文", "簡体", "简体"]

    for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=min(5, ws.max_row or 5), values_only=True), 1):
        found = {}
        for ci, cv in enumerate(row):
            cvs = str(cv).strip().lower() if cv else ""
            for kw in src_keywords:
                if kw in cvs:
                    found.setdefault("src", ci)
            for kw in tgt_keywords:
                if kw in cvs and ci != found.get("src"):
                    found.setdefault("tgt", ci)
        if "src" in found or "tgt" in found:
            src_i = found.get("src", 0)
            tgt_i = found.get("tgt", 1)
            if src_i == tgt_i:
                tgt_i = 1 if src_i != 1 else 2
            return src_i, tgt_i, row_idx

    return _col_idx(source_col or "A"), _col_idx(target_col or "B"), 0


def extract_xlsx(file_path: Path, source_col: str = "", target_col: str = "") -> list[dict]:
    """从 xlsx/xlsm 直接提取 source/target 对。"""
    import openpyxl
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb.active

    src_idx, tgt_idx, header_row = _detect_columns(ws, source_col, target_col)
    max_col = max(src_idx, tgt_idx) + 1

    entries = []
    for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_col=max_col, values_only=True), 1):
        if header_row and row_idx <= header_row:
            continue  # 跳过标题行及其之前的元数据行
        src = _cell(row, src_idx)
        tgt = _cell(row, tgt_idx)
        if not src or not tgt:
            continue
        entries.append({
            "source": src,
            "target": tgt,
            "context": f"{file_path.name}:Row{row_idx}",
            "file": file_path.name,
        })

    wb.close()
    return entries


# ── 通用 parse 管线（mqxliff/docx/txt 等） ────────────────────────

def parse_via_convert(file_path: Path, tmp_dir: Path) -> Path:
    """用 convert.py parse → JSON。"""
    output = tmp_dir / f"{file_path.stem}.json"
    subprocess.run([
        sys.executable, str(SCRIPT_DIR / "convert.py"), "parse",
        str(file_path),
        "--output", str(output),
        "--output-dir", str(tmp_dir),
    ], check=True, capture_output=True, text=True, encoding="utf-8")
    return output


# ── 主流程 ─────────────────────────────────────────────────────────

def main(src_dir: str, output: str, source_col: str = "", target_col: str = ""):
    src = Path(src_dir)
    out = Path(output)
    if not src.is_dir():
        print(f"❌ 目录不存在: {src}")
        sys.exit(1)

    all_exts = list(PIPELINE_EXTS | XLSX_EXTS)
    files = sorted([f for ext in all_exts for f in src.glob(f"*{ext}")],
                   key=lambda f: f.name)
    if not files:
        print(f"❌ 目录中没有支持的文件: {src}")
        sys.exit(1)

    print(f"📂 找到 {len(files)} 个文件")

    tm = TranslationMemory(str(out.resolve()))
    tm._entries = []
    tm._loaded = True
    total_pairs = 0

    with tempfile.TemporaryDirectory(prefix="tm_rebuild_") as tmp_dir:
        tmp = Path(tmp_dir)
        for f in files:
            file_entries = []
            line_count = 0

            if f.suffix.lower() in XLSX_EXTS:
                # xlsx/xlsm：直接读
                file_entries = extract_xlsx(f, source_col, target_col)
                line_count = len(file_entries)
            else:
                # mqxliff/docx/txt：走 convert.py parse 管线
                try:
                    json_path = parse_via_convert(f, tmp)
                except subprocess.CalledProcessError as e:
                    print(f"   ⚠️ {f.name}: parse 失败，跳过")
                    continue
                with open(json_path, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
                entries = data.get("entries", [])
                line_count = len(entries)
                for e in entries:
                    src_text = (e.get("source") or "").strip()
                    tgt_text = (e.get("target") or "").strip()
                    if not src_text or not tgt_text:
                        continue
                    file_entries.append({
                        "source": src_text,
                        "target": tgt_text,
                        "context": e.get("context", "") or e.get("note", "") or "",
                        "file": f.name,
                    })

            tm.add(file_entries, dedup=True)
            total_pairs += len(file_entries)
            status = f"{f.name}: {line_count} 条 → 提取 {len(file_entries)} 对"
            if len(file_entries) == 0 and f.suffix.lower() not in XLSX_EXTS:
                status += " ⚠️ 非 mqxliff 格式可能不提取译文，请用 --source-col/--target-col 或转为 mqxliff"
            print(f"   {status}")

    tm.save()
    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"\n✅ TM 重建完成: {out}")
    print(f"   总提取: {total_pairs} 对")
    print(f"   去重后: {len(tm)} 条")
    print(f"   文件大小: {size_mb:.2f} MB")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="从已翻译批次重建翻译记忆")
    p.add_argument("src_dir", help="已翻译批次目录")
    p.add_argument("--output", "-o", default="batch_translate/data/tm_memory.json",
                   help="输出 TM JSON 路径")
    p.add_argument("--source-col", default="", help="xlsx 源列字母（默认自动检测）")
    p.add_argument("--target-col", default="", help="xlsx 译文列字母（默认自动检测）")
    args = p.parse_args()
    main(args.src_dir, args.output, args.source_col, args.target_col)
