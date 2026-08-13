#!/usr/bin/env python3
"""翻译记忆：JSON 存储 + difflib 模糊检索 + n-gram 片段匹配。"""

import json, re, sys
from difflib import SequenceMatcher
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── 展示层标签归一化（不改数据，只统一 AI 看到的 TM 参考） ─────────

_DISPLAY_TAG_RE = re.compile(
    r"<tag\s+id=['\"][^'\"]+['\"]\s+type=['\"]([^'\"]*)['\"]\s+desc=['\"]([^'\"]*)['\"]\s*/>"
)
_DISPLAY_BARE_RE = re.compile(r"<(?!tag\b)(/?[a-zA-Z][a-zA-Z0-9]*(?:=[^>]*)?)>")
_CLOSE_NAME_ALIAS = {"斜体": "i", "粗体": "b", "下划线": "u", "颜色": "color", "字号": "size"}


def display_tags(text: str) -> str:
    """把 mqxliff 的 <tag .../> 与 TM 裸标签统一为可读 ⟨...⟩ 形式（仅展示）。

    例：<tag ... desc='⟨actor⟩'/> → ⟨actor⟩；desc='color结束' → ⟨/color⟩；
    <color=orange> → ⟨color=orange⟩；</i> → ⟨/i⟩；br → 换行符。
    """
    def _tag(m):
        typ, desc = m.group(1), m.group(2)
        if typ.startswith("/") or desc.endswith("结束"):
            name = desc[:-2]
            return f"⟨/{_CLOSE_NAME_ALIAS.get(name, name)}⟩"
        if desc == "换行":
            return "\n"
        return desc if desc.startswith("⟨") and desc.endswith("⟩") else f"⟨{desc}⟩"

    text = _DISPLAY_TAG_RE.sub(_tag, text)

    def _bare(m):
        inner = m.group(1)
        closing = inner.startswith("/")
        core = inner[1:] if closing else inner
        return f"⟨{'/' if closing else ''}{core}⟩"

    return _DISPLAY_BARE_RE.sub(_bare, text)


class TranslationMemory:
    """JSON 翻译记忆库。"""

    def __init__(self, json_path: str | Path):
        self._path = Path(json_path)
        self._entries: list[dict] = []
        self._loaded = False
        self._ngram_index: dict[str, set[int]] = {}
        self._ngram2_index: dict[str, set[int]] = {}
        self._norm_lens: list[int] = []

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

    def add(self, entries: list[dict], dedup: bool = True, replace: bool = False):
        """追加条目；dedup=True 按 (source, context) 去重。

        replace=True 时仅覆盖同键且同 file 来源的旧条目（用于提交后积累
        同一工作文件的确认译文）；同键但来源不同（如 Master 权威条目）不覆盖，
        避免工作译文顶掉更高优先级来源。
        """
        self.load()
        added: list[dict] = []
        if dedup:
            existing = {}
            for i, e in enumerate(self._entries):
                existing.setdefault(
                    (e.get("source", "").strip(), e.get("context", "").strip()), i
                )
            replaced = 0
            skipped_other_source = 0
            for e in entries:
                k = (e.get("source", "").strip(), e.get("context", "").strip())
                entry = {
                    "source": e.get("source", ""),
                    "target": e.get("target", ""),
                    "context": e.get("context", ""),
                    "file": e.get("file", ""),
                }
                idx = existing.get(k)
                if idx is None:
                    self._entries.append(entry)
                    existing[k] = len(self._entries) - 1
                    added.append(entry)
                elif replace:
                    old = self._entries[idx]
                    if (old.get("file", "") or "") == (entry.get("file", "") or ""):
                        self._entries[idx] = entry
                        replaced += 1
                    else:
                        skipped_other_source += 1
            if replaced:
                print(f"ℹ️ TM 更新 {replaced} 条同键旧译文（replace=True）")
            if skipped_other_source:
                print(f"ℹ️ TM 跳过 {skipped_other_source} 条同键不同来源条目（保留原来源，避免覆盖权威）")
        else:
            self._entries.extend(entries)
            added = list(entries)
        if added:
            self._index_entries(range(len(self._entries) - len(added), len(self._entries)))

    # ── 模糊检索 ──────────────────────────────────────────────────

    _tag_re = re.compile(r"<[^>]+>")

    def find_matches(
        self, source: str, threshold: float = 0.6, top_n: int = 3,
        query_context: str = "", pool3: int = 300, pool2: int = 500,
        full_scan_len: int = 6,
    ) -> list[dict]:
        self.load()
        if not self._entries or not source: return []
        qp = self._tag_re.sub("", source)
        def _ctx(c): return sum(1 for q, c2 in zip(query_context.split("."), c.split(".")) if q == c2) if query_context and c else 0
        plain = self._normalize(source)
        if len(self._ngram_skip_re.sub("", plain)) <= full_scan_len:
            cand_ids = range(len(self._entries))
        else:
            cand_ids = self._recall_candidate_ids(source, pool3, pool2)
        s = []
        m = SequenceMatcher(a=qp)
        for i in cand_ids:
            e = self._entries[i]
            ep = self._tag_re.sub("", e["source"]); m.set_seq2(ep)
            r = m.ratio()
            if r >= threshold: s.append({"source": e["source"], "target": e["target"], "similarity": round(r, 4), "context": e.get("context", ""), "file": e.get("file", ""), "_c": _ctx(e.get("context", ""))})
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
        self._norm_lens.clear()
        self._index_entries(range(len(self._entries)))

    def _index_entries(self, idxs):
        for idx in idxs:
            entry = self._entries[idx]
            plain = self._normalize(entry["source"])
            self._norm_lens.append(len(plain))
            for seg in self._ngram_skip_re.split(plain):
                if len(seg) < 2: continue
                if len(seg) >= 3:
                    for i in range(len(seg) - 2): self._ngram_index.setdefault(seg[i:i+3], set()).add(idx)
                for i in range(len(seg) - 1): self._ngram2_index.setdefault(seg[i:i+2], set()).add(idx)

    def _top_ngram_ids(self, text: str, n: int, limit: int) -> list[int]:
        """按 n-gram 命中计数取 top limit 个条目 id；同计数短条目优先（贴近 ratio）。"""
        grams = self._extract_ngrams(text, n=n)
        if not grams:
            return []
        idx = self._ngram_index if n == 3 else self._ngram2_index
        counts: dict[int, int] = {}
        for g in sorted(grams):  # 确定性：避免 set 哈希随机化影响截断边界
            for i in sorted(idx.get(g, ())):
                counts[i] = counts.get(i, 0) + 1
        return sorted(counts, key=lambda i: (-counts[i], self._norm_lens[i]))[:limit]

    def _recall_candidate_ids(self, source: str, pool3: int = 300, pool2: int = 500) -> list[int]:
        """3-gram 与 2-gram 各取候选池并集（2-gram 兜底短词/子串）。"""
        c3 = self._top_ngram_ids(source, 3, pool3)
        c2 = self._top_ngram_ids(source, 2, pool2)
        # 有序列表：同分条目保持与全量扫描一致的原始条目顺序
        return sorted(set(c3) | set(c2))

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
            for g in sorted(grams):  # 确定性：同计数候选顺序稳定
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
            fs = qp[match.a:match.a + match.size].strip()
            # 同片段去重：保留重叠度最高的那一条 TM 条目
            existing = next((r for r in results if r["fragment_source"] == fs), None)
            if existing:
                if overlap > existing.get("_overlap", 0):
                    existing["match_source"] = e["source"]
                    existing["match_target"] = e["target"]
                    existing["match_file"] = e.get("file", "")
                    existing["_overlap"] = overlap
                continue
            results.append({"fragment_source": fs, "match_source": e["source"], "match_target": e["target"], "match_file": e.get("file", ""), "_overlap": overlap})
        for r in results: del r["_overlap"]
        # 包含去重
        filtered = [r for r in results
                    if not any(r is not r2 and r["fragment_source"] in r2["fragment_source"] for r2 in results)]
        return filtered[:top_n]


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
