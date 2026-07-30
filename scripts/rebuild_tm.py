#!/usr/bin/env python3
"""用 translate 工作流的 parse 管线从已翻译批次重建 TM。

流程：
  1. convert.py parse 每个文件（mqxliff/xlsx/docx/txt等）→ JSON（tag 规范化、文本抽取与工作流一致）
  2. 从 JSON entries 提取 source/target → TranslationMemory.add()
  3. 保存 TM
"""
import sys, json, subprocess, tempfile
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
from tm_store import TranslationMemory

EXPORT_NAMESPACE = 'urn:oasis:names:tc:xliff:document:1.2'


def parse_file(file_path: Path, tmp_dir: Path) -> Path:
    """用 convert.py parse 解析任意格式到临时 JSON，返回 JSON 路径。"""
    output = tmp_dir / f"{file_path.stem}.json"
    subprocess.run([
        sys.executable, str(SCRIPT_DIR / "convert.py"), "parse",
        str(file_path),
        "--output", str(output),
        "--output-dir", str(tmp_dir),
    ], check=True, capture_output=True, text=True, encoding="utf-8")
    return output


def main(src_dir: str, output: str):
    src = Path(src_dir)
    out = Path(output)
    if not src.is_dir():
        print(f"❌ 目录不存在: {src}")
        sys.exit(1)

    extensions = [".mqxliff", ".xlsx", ".xlsm", ".docx", ".txt", ".csv", ".tsv"]
    files = sorted([f for ext in extensions for f in src.glob(f"*{ext}")],
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
            try:
                json_path = parse_file(f, tmp)
            except subprocess.CalledProcessError as e:
                print(f"   ⚠️ {f.name}: parse 失败，跳过 → {e.stderr[:120]}")
                continue

            with open(json_path, "r", encoding="utf-8") as jf:
                data = json.load(jf)

            entries = data.get("entries", [])
            file_entries = []
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
            print(f"   {f.name}: {len(entries)} 条 → 提取 {len(file_entries)} 对")

    tm.save()
    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"\n✅ TM 重建完成: {out}")
    print(f"   总提取: {total_pairs} 对")
    print(f"   去重后: {len(tm)} 条")
    print(f"   文件大小: {size_mb:.2f} MB")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("src_dir", help="已翻译批次目录")
    p.add_argument("--output", "-o", default="batch_translate/data/tm_memory.json",
                   help="输出 TM JSON 路径")
    args = p.parse_args()
    main(args.src_dir, args.output)
