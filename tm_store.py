#!/usr/bin/env python3
"""翻译记忆：JSON 存储 + difflib 模糊检索 + n-gram 片段匹配。"""

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
        self._entries: list[dict] = []
        self._loaded = False
        self._ngram_index: dict[str, set[int]] = {}

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

    def add(self, entries: list[dict], dedup: bool = True):
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
        self.load()
        if not self._entries or not source:
            return []
        query_plain = self._tag_re.sub("", source)

        def _ctx_score(ctx: str) -> int:
            if not query_context or not ctx:
                return 0
            q_parts = query_context.split(".")
            c_parts = ctx.split(".")
            n = 0
            for q, c in zip(q_parts, c_parts):
                if q == c: n += 1
                else: break
            return n

        scored = []
        matcher = SequenceMatcher(a=query_plain)
        for entry in self._entries:
            entry_plain = self._tag_re.sub("", entry["source"])
            matcher.set_seq2(entry_plain)
            ratio = matcher.ratio()
            if ratio >= threshold:
                scored.append({
                    "source": entry["source"],
                    "target": entry["target"],
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
        self._ngram_index.clear()
        for idx, entry in enumerate(self._entries):
            plain = self._tag_re.sub("", entry["source"])
            segments = self._ngram_skip_re.split(plain)
            for seg in segments:
                if len(seg) < n: continue
                for i in range(len(seg) - n + 1):
                    gram = seg[i:i + n]
                    self._ngram_index.setdefault(gram, set()).add(idx)

    def _extract_ngrams(self, text: str, n: int = 3) -> set[str]:
        plain = self._tag_re.sub("", text)
        segments = self._ngram_skip_re.split(plain)
        grams = set()
        for seg in segments:
            if len(seg) < n: continue
            for i in range(len(seg) - n + 1):
                grams.add(seg[i:i + n])
        return grams

    # ── 片段匹配 ──────────────────────────────────────────────────

    def find_fragment_matches(
        self, source: str, min_match_len: int = 5,
        top_n: int = 5, candidate_limit: int = 20,
    ) -> list[dict]:
        self.load()
        if not self._entries or not source or not self._ngram_index:
            return []

        query_grams = self._extract_ngrams(source)
        if not query_grams:
            return []

        candidate_scores: dict[int, int] = {}
        for gram in query_grams:
            for idx in self._ngram_index.get(gram, ()):
                candidate_scores[idx] = candidate_scores.get(idx, 0) + 1
        if not candidate_scores:
            return []

        top_candidates = sorted(candidate_scores.items(), key=lambda x: -x[1])[:candidate_limit]
        query_plain = self._tag_re.sub("", source)
        results: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for idx, _score in top_candidates:
            entry = self._entries[idx]
            entry_plain = self._tag_re.sub("", entry["source"])
            matcher = SequenceMatcher(None, query_plain, entry_plain)
            while True:
                match = matcher.find_longest_match(0, len(query_plain), 0, len(entry_plain))
                if match.size < min_match_len: break
                blocks = matcher.get_matching_blocks()
                for b in blocks:
                    if b.size < min_match_len: continue
                    frag_src = query_plain[b.a:b.a + b.size].strip()
                    key = (frag_src, entry["source"])
                    if key in seen: continue
                    seen.add(key)
                    sim = b.size / len(query_plain[b.a:b.a + b.size]) if b.size > 0 else 0
                    frag_tgt, conf = self._align_fragment_target(
                        entry["source"], entry["target"],
                        entry_plain, b.b, b.b + b.size,
                    )
                    results.append({
                        "fragment_source": frag_src,
                        "fragment_target": frag_tgt,
                        "fragment_target_confidence": conf,
                        "match_source": entry["source"],
                        "match_target": entry["target"],
                        "similarity": round(sim, 4),
                    })
                break
        results.sort(key=lambda x: (-x["similarity"], -len(x["fragment_source"])))
        return results[:top_n]

    # ── 片段译文对齐 ──────────────────────────────────────────────

    _split_tag_re = re.compile(r'(<actor>)|(<i>|</i>)|(<tag\s[^>]+/>)|(\n)')

    @staticmethod
    def _split_by_tags(source: str, target: str) -> list[tuple[str, str]]:
        src_parts = [p for p in TranslationMemory._split_tag_re.split(source) if p is not None]
        tgt_parts = [p for p in TranslationMemory._split_tag_re.split(target) if p is not None]
        n = max(len(src_parts), len(tgt_parts))
        segments = []
        for i in range(n):
            s = src_parts[i].strip() if i < len(src_parts) else ""
            t = tgt_parts[i].strip() if i < len(tgt_parts) else ""
            if s or t: segments.append((s, t))
        return segments

    def _align_fragment_target(
        self, src_full: str, tgt_full: str,
        src_plain: str, frag_start: int, frag_end: int,
    ) -> tuple[str, str]:
        tgt_plain = self._tag_re.sub("", tgt_full)
        src_total = len(src_plain)
        tgt_total = len(tgt_plain)
        if src_total == 0 or tgt_total == 0:
            return tgt_full, "full_sentence"
        if src_total < 20 and not self._split_tag_re.search(src_full):
            return tgt_full, "full_sentence"
        segs = self._split_by_tags(src_full, tgt_full)
        if segs:
            seg_src = [self._tag_re.sub("", s) for s, _ in segs]
            seg_tgt = [self._tag_re.sub("", t) for _, t in segs]
            accum = 0
            for s_plain, t_plain in zip(seg_src, seg_tgt):
                sl = len(s_plain); tl = len(t_plain)
                if sl == 0: continue
                if frag_end > accum and frag_start < accum + sl:
                    ls = max(0, frag_start - accum); le = min(sl, frag_end - accum)
                    if tl > 0:
                        ts = max(0, min(int(ls/sl*tl), tl-1))
                        te = max(ts+1, min(int(le/sl*tl), tl))
                        tgt = t_plain[ts:te].strip()
                        if tgt: return tgt, "high"
                accum += sl
        s = max(0, min(int(frag_start/src_total*tgt_total), tgt_total-1))
        e = max(s+1, min(int(frag_end/src_total*tgt_total), tgt_total))
        tgt = tgt_plain[s:e].strip()
        return tgt, "medium" if tgt else "full_sentence"


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
