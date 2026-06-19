#!/usr/bin/env python3
"""翻译记忆：JSON 存储 + difflib 模糊检索。"""

import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class TranslationMemory:
    """JSON 翻译记忆库。"""

    def __init__(self, json_path: str | Path):
        self._path = Path(json_path)
        self._entries: list[dict] = []  # [{source, target, context, file}]
        self._loaded = False

    # ── 加载 / 保存 ───────────────────────────────────────────────

    def load(self) -> list[dict]:
        if self._loaded:
            return self._entries

        if self._path.is_file():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._entries = data.get("entries", [])
            except (json.JSONDecodeError, KeyError):
                self._entries = []

        self._loaded = True
        return self._entries

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"entries": self._entries}, f, ensure_ascii=False, indent=2)

    # ── 增删 ──────────────────────────────────────────────────────

    def add(self, entries: list[dict], dedup: bool = True):
        """追加条目。dedup=True 时按 (source, context) 去重。"""
        self.load()
        if dedup:
            existing_keys = {(e["source"], e.get("context", "")) for e in self._entries}
            for entry in entries:
                key = (entry.get("source", "").strip(), entry.get("context", "").strip())
                if key not in existing_keys:
                    self._entries.append({
                        "source": entry.get("source", ""),
                        "target": entry.get("target", ""),
                        "context": entry.get("context", ""),
                        "file": entry.get("file", ""),
                    })
                    existing_keys.add(key)
        else:
            self._entries.extend(entries)

    # ── 模糊检索 ──────────────────────────────────────────────────

    _tag_re = re.compile(r"<[^>]+>")

    def find_matches(
        self, source: str, threshold: float = 0.6, top_n: int = 3,
        query_context: str = "",
    ) -> list[dict]:
        """
        返回与 source 相似度 >= threshold 的前 top_n 条匹配。
        内部用去 tag 纯文本做模糊比对，返回保留完整 tag 的 source/target。
        结果按相似度降序排列；相似度相同时 context 前缀匹配的条目优先。
        """
        self.load()
        if not self._entries or not source:
            return []

        # 比对用纯文本
        query_plain = self._tag_re.sub("", source)

        def _ctx_score(ctx: str) -> int:
            """context 逐段（. 分隔）前缀匹配段数。"""
            if not query_context or not ctx:
                return 0
            q_parts = query_context.split(".")
            c_parts = ctx.split(".")
            n = 0
            for q, c in zip(q_parts, c_parts):
                if q == c:
                    n += 1
                else:
                    break
            return n

        scored = []
        matcher = SequenceMatcher(a=query_plain)
        for entry in self._entries:
            entry_plain = self._tag_re.sub("", entry["source"])
            matcher.set_seq2(entry_plain)
            ratio = matcher.ratio()
            if ratio >= threshold:
                scored.append({
                    "source": entry["source"],     # 含 tag 的完整版
                    "target": entry["target"],     # 含 tag 的完整版
                    "similarity": round(ratio, 4),
                    "context": entry.get("context", ""),
                    "_ctx": _ctx_score(entry.get("context", "")),
                })

        scored.sort(key=lambda x: (-x["similarity"], -x["_ctx"]))
        for s in scored:
            del s["_ctx"]
        return scored[:top_n]

    def __len__(self) -> int:
        self.load()
        return len(self._entries)


# ── CLI ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="翻译记忆工具")
    p.add_argument("--file", type=str, required=True, help="TM JSON 路径")
    p.add_argument("--stats", action="store_true", help="显示统计")
    p.add_argument("--search", type=str, help="搜索匹配")
    p.add_argument("--threshold", type=float, default=0.6, help="匹配阈值")
    args = p.parse_args()

    tm = TranslationMemory(args.file)
    tm.load()

    if args.stats:
        print(f"总条目: {len(tm)}")
    elif args.search:
        matches = tm.find_matches(args.search, threshold=args.threshold)
        for m in matches:
            print(f"  [{m['similarity']:.2f}] {m['source'][:60]} → {m['target'][:60]}")
        if not matches:
            print("  (无匹配)")
    else:
        p.print_help()
