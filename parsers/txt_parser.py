"""txt 解析器：按行提取"""

import json
import sys
from pathlib import Path
from typing import Optional

def parse(filepath: Path, **opts) -> dict:
    """按行解析 txt 文件。"""
    lines = _read_lines(filepath)
    entries = []
    for i, line in enumerate(lines, 1):
        text = line.strip()
        if not text:
            continue
        entries.append({
            "id": str(i),
            "source": text,
            "context": f"{filepath.name}:L{i}",
            "note": "",
        })

    return {
        "source_file": filepath.name,
        "entries": entries,
    }


def write(original_path: Path, translations_json: str | Path,
          output_path: Optional[Path] = None) -> Path:
    """将译文写回 txt，保持行序。"""
    original_lines = _read_lines(original_path)

    with open(translations_json, "r", encoding="utf-8") as f:
        translations = json.load(f)

    target_map = {}
    if isinstance(translations, list):
        target_map = {str(r["id"]): r["target"] for r in translations}
    elif isinstance(translations, dict) and "entries" in translations:
        for e in translations["entries"]:
            if e.get("target"):
                target_map[str(e["id"])] = e["target"]

    result_lines = []
    ti = 1
    for line in original_lines:
        stripped = line.strip()
        if stripped and str(ti) in target_map:
            result_lines.append(target_map[str(ti)] + "\n")
            ti += 1
        elif not stripped:
            result_lines.append(line)
            ti += 1
        else:
            result_lines.append(line)
            ti += 1

    if output_path is None:
        output_path = original_path.with_stem(original_path.stem + "_translated")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(result_lines)
    return output_path


def _read_lines(path: Path) -> list[str]:
    encodings = ["utf-8", "utf-8-sig", "shift-jis", "cp932", "gbk"]
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                return f.readlines()
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"无法检测 txt 编码: {path}")
