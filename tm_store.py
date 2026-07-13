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
        self._ngram_index: dict[str, set[int]] = {}  # {3-gram: {entry_idx, ...}}

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
        self._build_ngram_index()
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

    # ── n-gram 倒排索引 ───────────────────────────────────────────

    _ngram_skip_re = re.compile(r"[\s\u3000\u0020,.!?;:()\[\]{}「」『』、。！？…\-]+")

    def _build_ngram_index(self, n: int = 3):
        """为所有 TM 条目的 source 构建字符 n-gram 倒排索引。"""
        self._ngram_index.clear()
        for idx, entry in enumerate(self._entries):
            plain = self._tag_re.sub("", entry["source"])
            # 按标点/空白切段，每段内滑窗取 n-gram
            segments = self._ngram_skip_re.split(plain)
            for seg in segments:
                if len(seg) < n:
                    continue
                for i in range(len(seg) - n + 1):
                    gram = seg[i:i + n]
                    if gram not in self._ngram_index:
                        self._ngram_index[gram] = set()
                    self._ngram_index[gram].add(idx)

    def _extract_ngrams(self, text: str, n: int = 3) -> set[str]:
        """从文本中提取 n-gram 集合。"""
        plain = self._tag_re.sub("", text)
        segments = self._ngram_skip_re.split(plain)
        grams = set()
        for seg in segments:
            if len(seg) < n:
                continue
            for i in range(len(seg) - n + 1):
                grams.add(seg[i:i + n])
        return grams

    # ── 片段匹配 ──────────────────────────────────────────────────

    def find_fragment_matches(
        self,
        source: str,
        min_match_len: int = 5,
        top_n: int = 5,
        candidate_limit: int = 20,
    ) -> list[dict]:
        """
        n-gram 索引驱动的片段匹配：把 source 拆为 3-gram，查索引找候选条目，
        用 LCS 提取实际匹配的连续片段，对齐到 target 获取翻译片段。
        返回 [{fragment_source, fragment_target, similarity, match_source}, ...]。
        """
        self.load()
        if not self._entries or not source or not self._ngram_index:
            return []

        # 1. 提取 query n-gram，统计每个候选条目的共享 n-gram 数
        query_grams = self._extract_ngrams(source)
        if not query_grams:
            return []

        candidate_scores: dict[int, int] = {}
        for gram in query_grams:
            for idx in self._ngram_index.get(gram, ()):
                candidate_scores[idx] = candidate_scores.get(idx, 0) + 1

        if not candidate_scores:
            return []

        # 取共享 n-gram 最多的 top candidate_limit 个候选
        top_candidates = sorted(
            candidate_scores.items(), key=lambda x: -x[1]
        )[:candidate_limit]

        query_plain = self._tag_re.sub("", source)
        results: list[dict] = []
        seen_fragments: set[tuple[str, str]] = set()

        # 2. 对每个候选条目用 LCS 提取匹配片段
        for idx, _score in top_candidates:
            entry = self._entries[idx]
            entry_plain = self._tag_re.sub("", entry["source"])

            # 用 find_longest_match 迭代提取最长公共子串
            matcher = SequenceMatcher(None, query_plain, entry_plain)
            remaining_q = list(range(len(query_plain)))

            while True:
                match = matcher.find_longest_match(
                    0, len(query_plain), 0, len(entry_plain)
                )
                if match.size < min_match_len:
                    break

                frag_src = query_plain[match.a:match.a + match.size].strip()
                if not frag_src or len(frag_src) < min_match_len:
                    # 前进跳过这个过短的匹配
                    # 用 get_matching_blocks 一次性获取所有匹配块
                    break

                # 获取所有 matching blocks 一次性处理
                blocks = matcher.get_matching_blocks()
                for b in blocks:
                    if b.size < min_match_len:
                        continue
                    frag_src = query_plain[b.a:b.a + b.size].strip()
                    key = (frag_src, entry["source"])
                    if key in seen_fragments:
                        continue
                    seen_fragments.add(key)

                    similarity = b.size / len(query_plain[b.a:b.a + b.size]) if b.size > 0 else 0
                    results.append({
                        "fragment_source": frag_src,
                        "match_source": entry["source"],
                        "match_target": entry["target"],
                        "similarity": round(similarity, 4),
                    })
                break  # get_matching_blocks 已获取所有块，跳出 while

        results.sort(key=lambda x: (-x["similarity"], -len(x["fragment_source"])))
        return results[:top_n]


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
