#!/usr/bin/env python3
"""翻译记忆：JSON 存储 + difflib 模糊检索 + n-gram 片段匹配。"""

import json, math, os, re, sys, tempfile
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
        self._ngram_doc_freq: dict[str, int] = {}
        self._ngram2_doc_freq: dict[str, int] = {}
        self._fragment_support_cache: dict[str, tuple[int, int]] = {}
        self._norm_lens: list[int] = []

    # ── 加载 / 保存 ───────────────────────────────────────────────

    def load(self) -> list[dict]:
        if self._loaded: return self._entries
        if self._path.is_file():
            try:
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
            except json.JSONDecodeError as exc:
                raise ValueError(f"TM JSON 损坏，拒绝覆盖: {self._path}: {exc}") from exc
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                raise ValueError(f"TM JSON 结构无效，拒绝覆盖: {self._path}")
            self._entries = data["entries"]
        self._loaded = True; self._build_ngram_index()
        return self._entries

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"entries": self._entries}, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_name, self._path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(temp_name).unlink(missing_ok=True)
            raise

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
        self._ngram_doc_freq.clear(); self._ngram2_doc_freq.clear()
        self._fragment_support_cache.clear()
        self._norm_lens.clear()
        self._index_entries(range(len(self._entries)))

    def _index_entries(self, idxs):
        for idx in idxs:
            entry = self._entries[idx]
            plain = self._normalize(entry["source"])
            self._norm_lens.append(len(plain))
            grams3: set[str] = set()
            grams2: set[str] = set()
            for seg in self._fragment_segments(entry["source"]):
                if len(seg) < 2: continue
                if len(seg) >= 3:
                    grams3.update(seg[i:i+3] for i in range(len(seg) - 2))
                grams2.update(seg[i:i+2] for i in range(len(seg) - 1))
            for gram in grams3:
                self._ngram_index.setdefault(gram, set()).add(idx)
                self._ngram_doc_freq[gram] = self._ngram_doc_freq.get(gram, 0) + 1
            for gram in grams2:
                self._ngram2_index.setdefault(gram, set()).add(idx)
                self._ngram2_doc_freq[gram] = self._ngram2_doc_freq.get(gram, 0) + 1

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
        grams = set()
        for seg in self._fragment_segments(text):
            if len(seg) < n: continue
            for i in range(len(seg) - n + 1): grams.add(seg[i:i+n])
        return grams

    def _fragment_segments(self, text: str) -> list[str]:
        """Normalize visible text without permitting a match across a tag."""
        bounded = self._tag_re.sub("\n", text).translate(self._FW_TO_HW)
        return [
            segment for segment in self._ngram_skip_re.split(bounded)
            if segment
        ]

    # ── 片段匹配 ──────────────────────────────────────────────────

    def find_fragment_matches(
        self, source: str, top_n: int = 5,
        candidate_limit: int = 200, exclude_sources: set[str] | None = None,
    ) -> list[dict]:
        """Find useful reusable fragments without matching across inline tags."""
        return self.debug_fragment_matches(
            source, top_n=top_n, candidate_limit=candidate_limit,
            exclude_sources=exclude_sources,
        )["matches"]

    def debug_fragment_matches(
        self, source: str, top_n: int = 5,
        candidate_limit: int = 200, exclude_sources: set[str] | None = None,
    ) -> dict:
        """Return fragment candidates, rejections, and deterministic score details."""
        self.load()
        self._fragment_support_cache.clear()
        if not self._entries or not source or (not self._ngram_index and not self._ngram2_index):
            return {"matches": [], "candidates": [], "rejected": []}

        def score_candidates(n: int) -> dict[int, float]:
            grams = self._extract_ngrams(source, n=n)
            index = self._ngram_index if n == 3 else self._ngram2_index
            frequencies = self._ngram_doc_freq if n == 3 else self._ngram2_doc_freq
            values: dict[int, float] = {}
            for gram in sorted(grams):
                idf = math.log((len(self._entries) + 1) / (frequencies.get(gram, 0) + 1)) + 1.0
                for idx in index.get(gram, ()):
                    values[idx] = values.get(idx, 0.0) + idf
            return values

        scores3 = score_candidates(3)
        scores2 = score_candidates(2)
        candidate_scores: dict[int, float] = {}
        for index, score in scores3.items():
            candidate_scores[index] = candidate_scores.get(index, 0.0) + score
        for index, score in scores2.items():
            candidate_scores[index] = candidate_scores.get(index, 0.0) + score
        if not candidate_scores:
            return {"matches": [], "candidates": [], "rejected": []}
        exclude = exclude_sources or set()
        query_segments = self._fragment_segments(source)
        query_len = max(1, sum(len(part) for part in query_segments))
        candidates = sorted(
            candidate_scores,
            key=lambda idx: (-candidate_scores[idx], self._norm_lens[idx], idx),
        )[:candidate_limit]
        results: list[dict] = []
        rejected: list[dict] = []
        for idx in candidates:
            e = self._entries[idx]
            diagnostic = {
                "source": e["source"], "file": e.get("file", ""),
                "ngram_score": round(candidate_scores[idx], 4),
            }
            if e["source"] in exclude:
                diagnostic["reason"] = "already_exact_match"
                rejected.append(diagnostic)
                continue
            best: tuple[int, str, str] | None = None
            for query_segment in query_segments:
                for entry_segment in self._fragment_segments(e["source"]):
                    match = SequenceMatcher(None, query_segment, entry_segment).find_longest_match(
                        0, len(query_segment), 0, len(entry_segment)
                    )
                    fragment = query_segment[match.a:match.a + match.size]
                    if best is None or match.size > best[0]:
                        best = (match.size, fragment, entry_segment)
            if best is None or best[0] < 4:
                diagnostic["reason"] = "fragment_too_short_or_no_boundary_safe_match"
                rejected.append(diagnostic)
                continue
            length, fragment, entry_segment = best
            if self._is_weak_fragment(fragment):
                diagnostic["reason"] = "weak_or_incomplete_fragment"
                diagnostic["fragment"] = fragment
                rejected.append(diagnostic)
                continue
            support_sources, support_files = self._fragment_support(fragment)
            coverage = length / query_len
            entry_coverage = length / max(1, len(entry_segment))
            score = (
                candidate_scores[idx]
                + length * 2.0
                + coverage * 10.0
                + entry_coverage * 2.0
                + support_sources * 1.25
                + support_files * 0.75
            )
            results.append({
                "fragment_source": fragment,
                "match_source": e["source"],
                "match_target": e["target"],
                "match_file": e.get("file", ""),
                "fragment_score": round(score, 4),
                "query_coverage": round(coverage, 4),
                "supporting_sources": support_sources,
                "supporting_files": support_files,
            })

        # Same fragment and same translation is redundant evidence.  Keep the
        # strongest evidence, while retaining genuinely conflicting translations.
        deduped: dict[tuple[str, str], dict] = {}
        for item in results:
            key = (item["fragment_source"], item["match_target"])
            old = deduped.get(key)
            if old is None or self._fragment_sort_key(item) < self._fragment_sort_key(old):
                deduped[key] = item
        ordered = sorted(deduped.values(), key=self._fragment_sort_key)
        filtered = [
            item for item in ordered
            if not any(
                item is not other
                and item["fragment_source"] in other["fragment_source"]
                and other["fragment_score"] >= item["fragment_score"]
                for other in ordered
            )
        ]
        return {
            "matches": filtered[:top_n],
            "candidates": [
                {"source": self._entries[idx]["source"], "file": self._entries[idx].get("file", ""),
                 "ngram_score": round(candidate_scores[idx], 4)}
                for idx in candidates
            ],
            "rejected": rejected,
        }

    @staticmethod
    def _fragment_sort_key(item: dict) -> tuple:
        return (
            -float(item.get("fragment_score", 0)),
            -int(item.get("supporting_files", 0)),
            -int(item.get("supporting_sources", 0)),
            -len(item.get("fragment_source", "")),
            item.get("match_file", ""), item.get("match_source", ""),
        )

    @staticmethod
    def _is_weak_fragment(fragment: str) -> bool:
        if len(fragment) < 4:
            return True
        # Isolated grammatical tails and partially consumed verb endings make
        # poor translation evidence even when their n-grams are frequent.
        return bool(
            re.search(r"^(?:して|として|また|この|その|あの|を|に|が|は|の|と|へ|で|も|や)", fragment)
            or re.search(r"(?:を|に|が|は|の|と|へ|で|も|や|て|し|い|る)$", fragment)
        )

    def _fragment_support(self, fragment: str) -> tuple[int, int]:
        cached = self._fragment_support_cache.get(fragment)
        if cached is not None:
            return cached
        sources: set[str] = set()
        files: set[str] = set()
        for entry in self._entries:
            if any(fragment in segment for segment in self._fragment_segments(entry["source"])):
                sources.add(entry["source"])
                if entry.get("file"):
                    files.add(str(entry["file"]))
        value = (len(sources), len(files))
        self._fragment_support_cache[fragment] = value
        return value


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
