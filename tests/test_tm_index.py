"""tm_store 索引召回测试：与全量一致性、截断回归、短查询兜底、add 增量、确定性。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tm_store import TranslationMemory, display_tags


def _write_tm(path: Path, entries: list[dict]) -> Path:
    path.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")
    return path


def _full_scan(tm: TranslationMemory, source: str, threshold: float = 0.6, top_n: int = 3):
    """旧全量逻辑，作为基准。"""
    qp = tm._tag_re.sub("", source)
    m = SequenceMatcher(a=qp)
    hits = []
    for e in tm._entries:
        m.set_seq2(tm._tag_re.sub("", e["source"]))
        r = m.ratio()
        if r >= threshold:
            hits.append((e["source"], round(r, 4)))
    hits.sort(key=lambda x: -x[1])
    return hits[:top_n]


class TmIndexTest(unittest.TestCase):
    def test_corrupt_tm_is_not_treated_as_empty_or_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tm.json"
            original = b'{"entries": ['
            path.write_bytes(original)
            tm = TranslationMemory(path)

            with self.assertRaises(ValueError):
                tm.add([{"source": "原文", "target": "译文"}])
            self.assertEqual(path.read_bytes(), original)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.tm_path = self.dir / "tm.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _tm(self, entries: list[dict]) -> TranslationMemory:
        _write_tm(self.tm_path, entries)
        tm = TranslationMemory(self.tm_path)
        tm.load()
        return tm

    def test_index_path_matches_full_scan_small_tm(self):
        """小 TM：强制索引路径（full_scan_len=0）结果与全量扫描一致。"""
        entries = [
            {"source": "装備とマテリア", "target": "装备与魔晶石", "context": "a", "file": "f"},
            {"source": "アイテムの説明", "target": "道具说明", "context": "b", "file": "f"},
            {"source": "全角テストＡＢＣ", "target": "全角测试ABC", "context": "", "file": "f"},
            {"source": "タグ付き<tag id='1'/>テキスト", "target": "带标签文本", "context": "", "file": "f"},
            {"source": "短い", "target": "短", "context": "", "file": "f"},
            {"source": "魔法", "target": "魔法", "context": "", "file": "f"},
        ]
        tm = self._tm(entries)
        queries = ["装備とマテリア", "アイテム", "全角テストABC", "タグ付きテキスト", "短い", "魔法"]
        for q in queries:
            got = [(m["source"], m["similarity"]) for m in
                   tm.find_matches(q, full_scan_len=0)]
            self.assertEqual(got, _full_scan(tm, q), f"query={q}")

    def test_substring_short_entry_survives_pool_truncation(self):
        """截断回归：600 条共享同组 n-gram 的长条目不应挤掉更短、相似度更高的词条。"""
        entries = []
        for i in range(600):
            entries.append({"source": f"マテリアルシステムの説明{i}", "target": "t", "context": "", "file": "f"})
        entries.append({"source": "装備とマテリア", "target": "装备与魔晶石", "context": "", "file": "f"})
        entries.append({"source": "マテリア", "target": "魔晶石", "context": "", "file": "f"})
        entries.append({"source": "魔法マテリア", "target": "魔法魔晶石", "context": "", "file": "f"})
        tm = self._tm(entries)
        got = [(m["source"], m["similarity"]) for m in tm.find_matches("装備とマテリア", full_scan_len=0)]
        self.assertEqual(
            got,
            [("装備とマテリア", 1.0), ("マテリア", 0.7273), ("魔法マテリア", 0.6154)],
        )
        self.assertEqual(got, _full_scan(tm, "装備とマテリア"))

    def test_short_query_full_scan_fallback(self):
        """短查询走全量兜底：索引池被故意缩小时，兜底仍返回与全量一致的结果。"""
        entries = []
        for i in range(600):
            entries.append({"source": f"マテリアルシステムの説明{i}", "target": "t", "context": "", "file": "f"})
        entries.append({"source": "装備とマテリア", "target": "装备与魔晶石", "context": "", "file": "f"})
        entries.append({"source": "マテリア", "target": "魔晶石", "context": "", "file": "f"})
        tm = self._tm(entries)
        # 池缩到 1/1 时索引路径必然丢匹配（证明截断真实存在）
        idx_only = tm.find_matches("装備とマテリア", full_scan_len=0, pool3=1, pool2=1)
        self.assertNotEqual(
            [(m["source"], m["similarity"]) for m in idx_only],
            _full_scan(tm, "装備とマテリア"),
        )
        # 短查询（"マテリア" 4 字符）默认走全量兜底，结果与全量一致
        got = [(m["source"], m["similarity"]) for m in tm.find_matches("マテリア")]
        self.assertEqual(got, _full_scan(tm, "マテリア"))
        self.assertEqual(got[0][0], "マテリア")

    def test_add_updates_index_incrementally(self):
        """add() 后同进程立即可查：整句与片段匹配都能命中。"""
        tm = self._tm([{"source": "既存のテキスト", "target": "既有文本", "context": "c1", "file": "f"}])
        tm.add([{"source": "新規テキストです", "target": "新文本", "context": "c2", "file": "f"}])
        self.assertEqual(len(tm._norm_lens), len(tm._entries))
        got = tm.find_matches("新規テキストです", threshold=1.0)
        self.assertEqual(got[0]["source"], "新規テキストです")
        tm.add([{"source": "これは新規に追加された長いテキストです", "target": "这是新追加的长文本", "context": "c3", "file": "f"}])
        frag = tm.find_fragment_matches("新規に追加された")
        self.assertTrue(frag)
        # 重复 add 不重复索引
        tm.add([{"source": "新規テキストです", "target": "新文本", "context": "c2", "file": "f"}])
        self.assertEqual(len(tm._entries), 3)
        self.assertEqual(len(tm._norm_lens), len(tm._entries))

    def test_add_no_dedup_keeps_index_aligned(self):
        """dedup=False 时条目与索引、长度数组保持对齐。"""
        tm = self._tm([{"source": "既存のテキスト", "target": "既有文本", "context": "", "file": "f"}])
        tm.add([{"source": "二本目", "target": "第二", "context": "", "file": "f"},
                {"source": "三本目", "target": "第三", "context": "", "file": "f"}], dedup=False)
        self.assertEqual(len(tm._entries), 3)
        self.assertEqual(len(tm._norm_lens), 3)
        self.assertEqual(tm.find_matches("二本目", threshold=1.0)[0]["source"], "二本目")

    def test_add_replace_false_keeps_old_target(self):
        """批量建库默认行为：同键旧译文优先保留，新译文不覆盖。"""
        tm = self._tm([{"source": "ＭＲ２５０になった", "target": "迈入大师等级250",
                        "context": "c1", "file": "旧文件"}])
        tm.add([{"source": "ＭＲ２５０になった", "target": "大师等级达到250级",
                 "context": "c1", "file": "新文件"}])
        self.assertEqual(len(tm._entries), 1)
        self.assertEqual(tm._entries[0]["target"], "迈入大师等级250")
        self.assertEqual(len(tm._norm_lens), len(tm._entries))

    def test_add_replace_true_updates_same_source(self):
        """提交积累使用 replace=True：同键且同 file 来源时新译文覆盖旧译文。"""
        tm = self._tm([{"source": "ＭＲ２５０になった", "target": "迈入大师等级250",
                        "context": "c1", "file": "_working_分割_zho-CN.mqxliff"}])
        tm.add([{"source": "ＭＲ２５０になった", "target": "大师等级达到250级",
                 "context": "c1", "file": "_working_分割_zho-CN.mqxliff"}], replace=True)
        self.assertEqual(len(tm._entries), 1)
        self.assertEqual(tm._entries[0]["target"], "大师等级达到250级")
        self.assertEqual(len(tm._norm_lens), len(tm._entries))
        got = tm.find_matches("ＭＲ２５０になった", threshold=1.0)
        self.assertEqual(got[0]["target"], "大师等级达到250级")

    def test_add_replace_true_keeps_other_source(self):
        """replace=True 不覆盖同键但来源不同的条目（保留 Master 等权威）。"""
        tm = self._tm([{"source": "オンライン設定", "target": "在线设定",
                        "context": "RefMenu_277_MR", "file": "EXP Master 2026-07-21"}])
        tm.add([{"source": "オンライン設定", "target": "在线设置",
                 "context": "RefMenu_277_MR", "file": "_working_分割_zho-CN.mqxliff"}],
               replace=True)
        self.assertEqual(len(tm._entries), 1)
        self.assertEqual(tm._entries[0]["target"], "在线设定")
        self.assertEqual(tm._entries[0]["file"], "EXP Master 2026-07-21")

    def test_fragment_ranking_prefers_complete_recurring_entity(self):
        tm = self._tm([
            {"source": "裏側の世界", "target": "里侧的世界", "context": "", "file": "a"},
            {"source": "裏側の世界へようこそ", "target": "欢迎来到里侧的世界", "context": "", "file": "b"},
            {"source": "世界の裏側", "target": "世界的背面", "context": "", "file": "c"},
            {"source": "ことができます", "target": "可以", "context": "", "file": "d"},
        ])

        debug = tm.debug_fragment_matches("裏側の世界で待っています")

        self.assertEqual(debug["matches"][0]["fragment_source"], "裏側の世界")
        self.assertEqual(debug["matches"][0]["match_target"], "里侧的世界")
        self.assertEqual(debug["matches"][0]["supporting_files"], 2)
        self.assertTrue(any(item["reason"] for item in debug["rejected"]))

    def test_fragment_matching_never_crosses_tag_boundaries(self):
        tm = self._tm([{
            "source": "裏側<tag id='1' type='fmt' desc='x'/>の世界",
            "target": "里侧的世界",
            "context": "",
            "file": "tagged",
        }])

        matches = tm.find_fragment_matches("裏側の世界で待つ")

        self.assertEqual(matches, [])

    def test_result_deterministic_across_hash_seeds(self):
        """跨进程 PYTHONHASHSEED 不同时，截断边界不因 set 哈希随机化漂移。"""
        entries = []
        for i in range(600):
            entries.append({"source": f"マテリアルシステムの説明{i}", "target": "t", "context": "", "file": "f"})
        entries.append({"source": "装備とマテリア", "target": "装备与魔晶石", "context": "", "file": "f"})
        entries.append({"source": "マテリア", "target": "魔晶石", "context": "", "file": "f"})
        entries.append({"source": "魔法マテリア", "target": "魔法魔晶石", "context": "", "file": "f"})
        _write_tm(self.tm_path, entries)
        code = (
            "import json,sys; sys.path.insert(0, %r); "
            "from tm_store import TranslationMemory; "
            "tm=TranslationMemory(sys.argv[1]); tm.load(); "
            "print(json.dumps(tm.find_matches(sys.argv[2], full_scan_len=0), ensure_ascii=False))"
        ) % str(ROOT)
        outputs = []
        for seed in ("1", "2", "42"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            r = subprocess.run(
                [sys.executable, "-c", code, str(self.tm_path), "装備とマテリア"],
                capture_output=True, text=True, env=env, cwd=str(ROOT), encoding="utf-8",
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            outputs.append(r.stdout.strip())
        self.assertEqual(len(set(outputs)), 1)
        first = json.loads(outputs[0])
        self.assertEqual([m["source"] for m in first],
                         ["装備とマテリア", "マテリア", "魔法マテリア"])

    def test_display_tags_unifies_mqxliff_and_bare_formats(self):
        """展示层把 mqxliff <tag .../> 与 TM 裸标签统一为 ⟨...⟩ 形式。"""
        mq = (
            "<tag id='1' type='fmt' desc='⟨actor⟩'/>"
            "<tag id='2' type='fmt' desc='⟨color=orange⟩'/>本文"
            "<tag id='3' type='/fmt' desc='color结束'/>"
            "<tag id='4' type='br' desc='换行'/>"
            "<tag id='5' type='/fmt' desc='斜体结束'/>"
        )
        past = "<actor><color=orange>本文</color>\n<i>斜</i>"
        self.assertEqual(
            display_tags(mq),
            "⟨actor⟩⟨color=orange⟩本文⟨/color⟩\n⟨/i⟩",
        )
        self.assertEqual(
            display_tags(past),
            "⟨actor⟩⟨color=orange⟩本文⟨/color⟩\n⟨i⟩斜⟨/i⟩",
        )

    def test_display_tags_leaves_plain_text_untouched(self):
        """普通文本与 & 不受展示转换影响。"""
        self.assertEqual(display_tags("MATERIA&EQUIPMENT"), "MATERIA&EQUIPMENT")
        self.assertEqual(display_tags("装備とマテリア"), "装備とマテリア")


if __name__ == "__main__":
    unittest.main()
