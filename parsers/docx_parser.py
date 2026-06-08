"""docx 解析器：按段落提取，保留格式信息"""

import json
import sys
from pathlib import Path
from typing import Optional

try:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except ImportError:
    print("错误: 需要 python-docx，请执行 pip install python-docx")
    sys.exit(1)


def parse(filepath: Path, **opts) -> dict:
    """解析 docx，提取段落文本和上下文。"""
    doc = Document(str(filepath))

    entries = []
    heading_stack = []
    para_idx = 0

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        para_idx += 1

        # 标题层级
        if para.style and para.style.name and "Heading" in para.style.name:
            level = para.style.name.replace("Heading ", "").strip()
            heading_stack = [h for h in heading_stack if not h.startswith(f"H{level}")]
            heading_stack.append(f"H{level}:{text}")

        context = " > ".join(heading_stack) if heading_stack else filepath.name

        # 格式信息
        format_info = []
        if para.style and para.style.name:
            format_info.append(para.style.name)
        runs_info = []
        for run in para.runs:
            if run.bold:
                runs_info.append("bold")
            if run.italic:
                runs_info.append("italic")
            if run.underline:
                runs_info.append("underline")
        if runs_info:
            format_info.append(", ".join(sorted(set(runs_info))))

        # source 含内联格式标记
        source_parts = []
        for run in para.runs:
            t = run.text
            if not t:
                continue
            if run.bold:
                source_parts.append(f"<tag id='b{para_idx}' type='fmt' desc='粗体开始'/>")
                source_parts.append(t)
                source_parts.append(f"<tag id='/b{para_idx}' type='/fmt' desc='粗体结束'/>")
            elif run.italic:
                source_parts.append(f"<tag id='i{para_idx}' type='fmt' desc='斜体开始'/>")
                source_parts.append(t)
                source_parts.append(f"<tag id='/i{para_idx}' type='/fmt' desc='斜体结束'/>")
            else:
                source_parts.append(t)

        source = "".join(source_parts) if source_parts else text

        entries.append({
            "id": str(para_idx),
            "source": source,
            "context": context,
            "note": "; ".join(format_info) if format_info else "",
        })

    # 表格
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if not any(cells):
                continue
            para_idx += 1
            source = cells[0] if cells else ""
            note = " | ".join(cells[1:]) if len(cells) > 1 else ""
            entries.append({
                "id": str(para_idx),
                "source": source,
                "context": f"Table:{filepath.name}",
                "note": f"Table cell; {note}" if note else "Table cell",
            })

    return {
        "source_file": filepath.name,
        "entries": entries,
    }


def write(original_path: Path, translations_json: str | Path,
          output_path: Optional[Path] = None) -> Path:
    """将译文写回 docx，替换段落文本。"""
    with open(translations_json, "r", encoding="utf-8") as f:
        translations = json.load(f)

    target_map = {}
    if isinstance(translations, list):
        target_map = {str(r["id"]): r["target"] for r in translations}
    elif isinstance(translations, dict) and "entries" in translations:
        for e in translations["entries"]:
            if e.get("target"):
                target_map[str(e["id"])] = e["target"]

    import re
    tag_re = re.compile(r"<tag[^>]*/>")

    doc = Document(str(original_path))
    para_idx = 0

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        para_idx += 1
        tid = str(para_idx)
        if tid in target_map:
            plain = tag_re.sub("", target_map[tid])
            para.text = plain

    # 表格
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if not any(cells):
                continue
            para_idx += 1
            tid = str(para_idx)
            if tid in target_map and row.cells:
                plain = tag_re.sub("", target_map[tid])
                row.cells[0].text = plain

    if output_path is None:
        output_path = original_path.with_stem(original_path.stem + "_translated")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path
