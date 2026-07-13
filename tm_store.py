#!/usr/bin/env python3
"""翻译记忆：JSON 存储 + difflib 模糊检索 + n-gram 片段匹配（3-gram + 2-gram 降级 + 全半角归一化）。"""

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
        if self._loaded:
            return self._entries
        if self._path.is_file():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._entries = json.load(f).get("entries", [])
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
            existing = {(e["source"], e.get("context", "")) for e in self._entries}
            for entry in entries:
                k = (entry.get("source", "").strip(), entry.get("context", "").strip())
                if k not in existing:
                    self._entries.append({"source": entry.get("source", ""), "target": entry.get("target", ""), "context": entry.get("context", ""), "file": entry.get("file", "")})
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
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    )

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

    def find_fragment_matches(self, source: str, min_match_len: int = 5, top_n: int = 5, candidate_limit: int = 20) -> list[dict]:
        self.load()
        if not self._entries or not source or (not self._ngram_index and not self._ngram2_index): return []

        # 先走 3-gram，再降级 2-gram
        def _get_candidates(ngram_idx, grams):
            cs: dict[int, int] = {}
            for g in grams:
                for idx in ngram_idx.get(g, ()): cs[idx] = cs.get(idx, 0) + 1
            return cs

        candidate_scores = _get_candidates(self._ngram_index, self._extract_ngrams(source))
        if not candidate_scores and self._ngram2_index:
            candidate_scores = _get_candidates(self._ngram2_index, self._extract_ngrams(source, n=2))
        if not candidate_scores: return []

        top_candidates = sorted(candidate_scores.items(), key=lambda x: -x[1])[:candidate_limit]
        qp = self._tag_re.sub("", source)
        results: list[dict] = []; seen: set = set()

        for idx, _ in top_candidates:
            e = self._entries[idx]; ep = self._tag_re.sub("", e["source"])
            m = SequenceMatcher(None, qp, ep)
            while True:
                match = m.find_longest_match(0, len(qp), 0, len(ep))
                if match.size < min_match_len: break
                for b in m.get_matching_blocks():
                    if b.size < min_match_len: continue
                    fs = qp[b.a:b.a+b.size].strip()
                    if (fs, e["source"]) in seen: continue
                    seen.add((fs, e["source"]))
                    sim = b.size / len(qp[b.a:b.a+b.size]) if b.size else 0
                    ft, cf = self._align_fragment_target(e["source"], e["target"], ep, b.b, b.b+b.size)
                    results.append({"fragment_source": fs, "fragment_target": ft, "fragment_target_confidence": cf, "match_source": e["source"], "match_target": e["target"], "similarity": round(sim, 4)})
                break
        results.sort(key=lambda x: (-x["similarity"], -len(x["fragment_source"])))
        # 包含去重：过滤掉被更长片段完全包含的短片段
        filtered = []
        for r in results:
            if not any(r is not r2 and r["fragment_source"] in r2["fragment_source"] for r2 in results):
                filtered.append(r)
        return filtered[:top_n]

    # ── 片段译文对齐 ──────────────────────────────────────────────

    _split_tag_re = re.compile(r'(<actor>)|(<i>|</i>)|(<tag\s[^>]+/>)|(\n)')

    @staticmethod
    def _split_by_tags(source: str, target: str) -> list[tuple[str, str]]:
        sp = [p for p in TranslationMemory._split_tag_re.split(source) if p is not None]
        tp = [p for p in TranslationMemory._split_tag_re.split(target) if p is not None]
        n = max(len(sp), len(tp)); segs = []
        for i in range(n):
            s = sp[i].strip() if i < len(sp) else ""; t = tp[i].strip() if i < len(tp) else ""
            if s or t: segs.append((s, t))
        return segs

    def _align_fragment_target(self, src_full: str, tgt_full: str, src_plain: str, frag_start: int, frag_end: int) -> tuple[str, str]:
        tgt_plain = self._tag_re.sub("", tgt_full)
        st, tt = len(src_plain), len(tgt_plain)
        if st == 0 or tt == 0: return tgt_full, "full_sentence"
        if st < 20 and not self._split_tag_re.search(src_full): return tgt_full, "full_sentence"
        segs = self._split_by_tags(src_full, tgt_full)
        if segs:
            ss = [self._tag_re.sub("", s) for s, _ in segs]; ts = [self._tag_re.sub("", t) for _, t in segs]
            acc = 0
            for sp, tp in zip(ss, ts):
                sl, tl = len(sp), len(tp)
                if sl == 0: continue
                if frag_end > acc and frag_start < acc + sl:
                    ls, le = max(0, frag_start - acc), min(sl, frag_end - acc)
                    if tl > 0:
                        t_s = max(0, min(int(ls/sl*tl), tl-1)); t_e = max(t_s+1, min(int(le/sl*tl), tl))
                        tgt = tp[t_s:t_e].strip()
                        if tgt: return tgt, "high"
                acc += sl
        s = max(0, min(int(frag_start/st*tt), tt-1)); e = max(s+1, min(int(frag_end/st*tt), tt))
        tgt = tgt_plain[s:e].strip()
        return tgt, "medium" if tgt else "full_sentence"


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True); p.add_argument("--stats", action="store_true")
    p.add_argument("--search"); p.add_argument("--threshold", type=float, default=0.6)
    a = p.parse_args()
    tm = TranslationMemory(a.file); tm.load()
    if a.stats: print(f"总条目: {len(tm)} | 3-gram: {len(tm._ngram_index)} | 2-gram: {len(tm._ngram2_index)}")
    elif a.search:
        for m in tm.find_matches(a.search, threshold=a.threshold):
            print(f"  [{m['similarity']:.2f}] {m['source'][:60]} → {m['target'][:60]}")
        print("  (无匹配)" if not tm.find_matches(a.search, threshold=a.threshold) else "")
    else: p.print_help()
