"""mqxliff 解析器：封装现有 mqxliff_tool.py"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional


def parse(filepath: Path, **opts) -> dict:
    """调用 mqxliff_tool.py export 生成中间 JSON。"""
    script = Path(__file__).resolve().parent.parent / "mqxliff_tool.py"
    output_dir = Path(__file__).resolve().parent.parent / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 构建 export 命令
    args = [
        sys.executable, str(script), "export",
        str(filepath),
        "--output", str(output_dir),
    ]
    if opts.get("terms"):
        args += ["--terms", opts["terms"]]
    if opts.get("tm"):
        args += ["--tm", opts["tm"]]
    if opts.get("style_guide"):
        args += ["--style-guide", opts["style_guide"]]

    subprocess.run(args, check=True)

    # export 输出到 exports/{stem}.json
    output_json = output_dir / f"{filepath.stem}.json"
    with open(output_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    return {
        "source_file": filepath.name,
        "entries": data.get("entries", []),
        "style_guide": data.get("style_guide"),
    }


def write(original_path: Path, translations_json: str | Path,
          output_path: Optional[Path] = None) -> Path:
    """调用 mqxliff_tool.py import 写回。"""
    script = Path(__file__).resolve().parent.parent / "mqxliff_tool.py"

    if output_path is None:
        output_path = original_path.with_stem(original_path.stem + "_translated")

    args = [
        sys.executable, str(script), "import",
        str(translations_json),
        str(original_path),
        "--output", str(output_path),
    ]

    subprocess.run(args, check=True)
    return output_path
