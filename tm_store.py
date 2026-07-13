#!/usr/bin/env python3
"""翻译记忆：JSON 存储 + difflib 模糊检索 + n-gram 片段匹配。"""

import json, re, sys
from difflib import SequenceMatcher
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

class TranslationMemory:
    """JSON 翻译记忆库。"""

    def __init__(self, json_path: str | Path):
        self._path = Path(json_path)
        self._entries: list[dict] = []
        self._loaded = False
        self._ngram_index: dict[str, set[int]] = {}
        self._ngram2_index: dict[str, set[int]] = {}

    # ── 加载 / 保存 ───────────────────────────────────────────────

    def load(self) -> list[dict]:
        if self._loaded: return self._entries
        if self._path.is_file():
            try:
                self._entries = json.load(open(self._path, encoding="utf-8")).get("entries", [])
            except (json.JSONDecodeError, KeyError):
                self._entries = []
        self._loaded = True; self._build_ngram_index()
        return self._entries

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        json.dump({"entries": self._entries}, open(self._path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    def add(self, entries: list[dict], dedup: bool = True):
        self.load()
        if dedup:
            existing = {(e["source"], e.get("context", "")) for e in self._entries}
            for e in entries:
                k = (e.get("source", "").strip(), e.get("context", "").strip())
                if k not in existing:
                    self._entries.append({"source": e.get("source", ""), "target": e.get("target", ""), "context": e.get("context", ""), "file": e.get("file", "")})
                    existing.add(k)
        else:
            self._entries.extend(entries)

    # ── 模糊检索 ──────────────────────────────────────────────────

    _tag_re = re.compile(r"<[^>]+>")

    def find_matches(self, source: str, threshold: float = 0.6, top_n: int = 3, query_context: str = "") -> list[dict]:
        self.load()
        if not self._entries or not source: return []
        qp = self._tag_re.sub("", source)
        def _ctx(c): return sum(1 for q, c2 in zip(query_context.split("."), c.split(".")) if q == c2) if query_context and c else 0
        s = []
        m = SequenceMatcher(a=qp)
        for e in self._entries:
            ep = self._tag_re.sub("", e["source"]); m.set_seq2(ep)
            r = m.ratio()
            if r >= threshold: s.append({"source": e["source"], "target": e["target"], "similarity": round(r, 4), "context": e.get("context", ""), "_c": _ctx(e.get("context", ""))})
        s.sort(key=lambda x: (-x["similarity"], -x["_c"]))
        for x in s: del x["_c"]
        return s[:top_n]

    def __len__(self) -> int: self.load(); return len(self._entries)

    # ── n-gram 倒排索引 ───────────────────────────────────────────

    _ngram_skip_re = re.compile(r"[\s\u3000\u0020,.!?;:()\[\]{}「」『』、。！？…\-]+")

    _FW_TO_HW = str.maketrans(
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
        "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
        "０１２３４５６７８９＂＃＄％＆＇（）＊＋，－．／：；＜＝＞？＠［＼］＾＿｀｛｜｝～",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ""abcdefghijklmnopqrstuvwxyz"
        "0123456789\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")

    @classmethod
    def _normalize(cls, text: str) -> str:
        return cls._tag_re.sub("", text).translate(cls._FW_TO_HW)

    def _build_ngram_index(self):
        self._ngram_index.clear(); self._ngram2_index.clear()
        for idx, entry in enumerate(self._entries):
            plain = self._normalize(entry["source"])
            for seg in self._ngram_skip_re.split(plain):
                if len(seg) < 2: continue
                if len(seg) >= 3:
                    for i in range(len(seg) - 2): self._ngram_index.setdefault(seg[i:i+3], set()).add(idx)
                for i in range(len(seg) - 1): self._ngram2_index.setdefault(seg[i:i+2], set()).add(idx)

    def _extract_ngrams(self, text: str, n: int = 3) -> set[str]:
        plain = self._normalize(text)
        grams = set()
        for seg in self._ngram_skip_re.split(plain):
            if len(seg) < n: continue
            for i in range(len(seg) - n + 1): grams.add(seg[i:i+n])
        return grams

    # ── 片段匹配 ──────────────────────────────────────────────────

    def find_fragment_matches(
        self, source: str, top_n: int = 5,
        candidate_limit: int = 20, exclude_sources: set[str] | None = None,
    ) -> list[dict]:
        """用 n-gram 索引找到共享子串最多的 TM 条目（已从整句匹配中排除）。
        不截取片段——AI 自行对照完整 source/target 判断对应。"""
        self.load()
        if not self._entries or not source or (not self._ngram_index and not self._ngram2_index):
            return []

        def _get(ngram_idx, grams):
            cs: dict[int, int] = {}
            for g in grams:
                for idx in ngram_idx.get(g, ()): cs[idx] = cs.get(idx, 0) + 1
            return cs

        candidate_scores = _get(self._ngram_index, self._extract_ngrams(source))
        if not candidate_scores and self._ngram2_index:
            candidate_scores = _get(self._ngram2_index, self._extract_ngrams(source, n=2))
        if not candidate_scores: return []

        exclude = exclude_sources or set()
        # n-gram 找候选 → LCS 验证实质性重叠
        qp = self._tag_re.sub("", source)
        results = []
        for idx, count in sorted(candidate_scores.items(), key=lambda x: -x[1])[:candidate_limit]:
            e = self._entries[idx]
            if e["source"] in exclude: continue
            ep = self._tag_re.sub("", e["source"])
            if len(ep) < 10: continue
            # LCS 最长匹配块 / 条目长度 → 重叠度
            m = SequenceMatcher(None, qp, ep)
            match = m.find_longest_match(0, len(qp), 0, len(ep))
            overlap = match.size / len(ep) if len(ep) > 0 else 0
            if overlap < 0.3: continue
            if e["source"] in {r["match_source"] for r in results}: continue
            results.append({"match_source": e["source"], "match_target": e["target"]})
        return results[:top_n]


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True); p.add_argument("--stats", action="store_true")
    p.add_argument("--search"); p.add_argument("--threshold", type=float, default=0.6)
    a = p.parse_args()
    tm = TranslationMemory(a.file); tm.load()
    if a.stats: print(f"总条目: {len(tm)} | 3g: {len(tm._ngram_index)} | 2g: {len(tm._ngram2_index)}")
    elif a.search:
        for m in tm.find_matches(a.search, threshold=a.threshold):
            print(f"  [{m['similarity']:.2f}] {m['source'][:60]} → {m['target'][:60]}")
        if not tm.find_matches(a.search, threshold=a.threshold): print("  (无匹配)")
    else: p.print_help()
