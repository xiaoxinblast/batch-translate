#!/usr/bin/env python3
"""格式转换层：统一 parse/write 接口，按扩展名路由到对应 parser。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from parsers import txt_parser, xlsx_parser, docx_parser, mqxliff_parser

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_FORMAT_MAP = {
    ".txt": ("txt", txt_parser),
    ".xlsx": ("xlsx", xlsx_parser),
    ".xlsm": ("xlsx", xlsx_parser),
    ".csv": ("txt", txt_parser),   # csv 当 txt 按行处理
    ".tsv": ("txt", txt_parser),
    ".docx": ("docx", docx_parser),
    ".mqxliff": ("mqxliff", mqxliff_parser),
}


def detect_format(filepath: Path) -> tuple[str, object]:
    ext = filepath.suffix.lower()
    if ext in _FORMAT_MAP:
        return _FORMAT_MAP[ext]
    raise ValueError(f"不支持的格式: {ext}")


def cmd_parse(args):
    filepath = Path(args.file)
    fmt_name, parser = detect_format(filepath)

    opts = {}
    if fmt_name == "xlsx":
        opts["source_col"] = args.source_col
        opts["target_col"] = args.target_col
        opts["header_row"] = args.header_row
    if getattr(args, "output_dir", None):
        opts["output_dir"] = args.output_dir

    data = parser.parse(filepath, **opts)
    data["_format"] = fmt_name

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"✅ 已解析 {len(data['entries'])} 条 → {out_path}")
    else:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2)

    return data


def cmd_write(args):
    filepath = Path(args.file)
    fmt_name, parser = detect_format(filepath)
    output = Path(args.output) if args.output else None

    result = parser.write(filepath, args.translations, output_path=output)
    print(f"✅ 已写回 → {result}")


def main():
    parser = argparse.ArgumentParser(description="格式转换层")
    sub = parser.add_subparsers(dest="command")

    p_parse = sub.add_parser("parse", help="解析源文件 → 中间 JSON")
    p_parse.add_argument("file", type=str, help="源文件路径")
    p_parse.add_argument("--output", "-o", type=str, default=None, help="输出 JSON 路径")
    p_parse.add_argument("--output-dir", type=str, default=None, help="mqxliff 导出目录")
    p_parse.add_argument("--source-col", type=str, default="A", help="xlsx 源列（默认 A）")
    p_parse.add_argument("--target-col", type=str, default="B", help="xlsx 目标列（默认 B）")
    p_parse.add_argument("--header-row", type=int, default=1, help="xlsx 表头行号（默认 1）")

    p_write = sub.add_parser("write", help="译文写回原格式")
    p_write.add_argument("file", type=str, help="原始文件路径")
    p_write.add_argument("translations", type=str, help="译文 JSON 路径")
    p_write.add_argument("--output", "-o", type=str, default=None, help="输出路径")

    args = parser.parse_args()

    if args.command == "parse":
        cmd_parse(args)
    elif args.command == "write":
        cmd_write(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
