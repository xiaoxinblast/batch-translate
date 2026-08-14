"""mqxliff 写回（含裸 & 转义）回归测试。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mqxliff_tool as mt


def _fixture_xml() -> str:
    return """<?xml version='1.0' encoding='UTF-8'?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" xmlns:mq="MQXliff" version="1.2">
<file original="t" source-language="ja" target-language="zh-cn" datatype="x-memoq">
<body>
<trans-unit id="1" mq:status="NotStarted" mq:locked="no">
<source xml:space="preserve">メインメニュー「MATERIA&amp;EQUIPMENT」<ph id="1">&lt;mq:ch val="&#10;" /&gt;</ph>を押す</source>
<target xml:space="preserve"></target>
</trans-unit>
<trans-unit id="2" mq:status="NotStarted" mq:locked="no">
<source xml:space="preserve">（う……）<ph id="2">&lt;mq:rxt displaytext="&amp;lt;actor&amp;gt;" val="&amp;lt;actor&amp;gt;" /&gt;</ph>クラウド</source>
<target xml:space="preserve"></target>
</trans-unit>
</body>
</file>
</xliff>"""


class MqxliffWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.src = self.td / "t.mqxliff"
        self.src.write_text(_fixture_xml(), encoding="utf-8")
        self.units, self.tree = mt.parse_mqxliff(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def test_raw_ampersand_roundtrip(self):
        """正文含裸 & 的译文写回后仍是 &，且不把标签写成字面文本。"""
        tr = {"1": "在主菜单“MATERIA&EQUIPMENT”<tag id='1' type='br' desc='换行'/>按"}
        out = self.td / "out.mqxliff"
        mt.write_translations(self.tree, self.units, tr, output_path=out)

        units2, _ = mt.parse_mqxliff(out)
        t1 = next(u for u in units2 if u.id == "1")
        self.assertIn("在主菜单“MATERIA&EQUIPMENT”", t1.target_text)
        self.assertIn("<tag id='1' type='br' desc='换行'/>", t1.target_text)
        raw = out.read_text(encoding="utf-8")
        self.assertIn("MATERIA&amp;EQUIPMENT", raw)
        self.assertNotIn("&lt;ph ", raw)

    def test_explicit_empty_target_clears_existing_translation(self):
        src = self.td / "existing.mqxliff"
        src.write_text(
            _fixture_xml().replace(
                '<target xml:space="preserve"></target>',
                '<target xml:space="preserve">旧译文</target>',
                1,
            ),
            encoding="utf-8",
        )
        units, tree = mt.parse_mqxliff(src)
        out = self.td / "cleared.mqxliff"

        mt.write_translations(tree, units, {"1": ""}, output_path=out)

        written, _ = mt.parse_mqxliff(out)
        self.assertEqual(next(unit for unit in written if unit.id == "1").target_text, "")

    def test_bare_actor_tag_normalized(self):
        """裸 <actor> 会被归一化为源文件的 ph 元素并保留。"""
        tr = {"2": "<actor>"}
        out = self.td / "out2.mqxliff"
        mt.write_translations(self.tree, self.units, tr, output_path=out)

        units2, _ = mt.parse_mqxliff(out)
        t2 = next(u for u in units2 if u.id == "2")
        self.assertEqual(t2.target_text, "<tag id='2' type='fmt' desc='⟨actor⟩'/>")

    def test_tag_first_target_keeps_order(self):
        """译文以 <tag .../> 开头时，写回后文本必须仍位于标签之后。"""
        tr = {"1": "<tag id='1' type='br' desc='换行'/>メニュー"}
        out = self.td / "out_tag_first.mqxliff"
        mt.write_translations(self.tree, self.units, tr, output_path=out)

        units2, _ = mt.parse_mqxliff(out)
        t1 = next(u for u in units2 if u.id == "1")
        self.assertEqual(t1.target_text, "<tag id='1' type='br' desc='换行'/>メニュー")

    def test_bare_close_tags_normalized(self):
        """裸结束标签 </color>/</i> 按中文 desc 反向还原为 /fmt 标签。"""
        tag_map = {
            "1": mt.InlineTag(
                ph_id="1", tag_type="fmt", desc="⟨color=orange⟩",
                original_ph_xml="<ph id='1'>&lt;mq:rxt displaytext=&quot;&amp;lt;color=orange&amp;gt;&quot; /&gt;</ph>",
            ),
            "2": mt.InlineTag(
                ph_id="2", tag_type="/fmt", desc="color结束",
                original_ph_xml="<ph id='2'>&lt;mq:rxt displaytext=&quot;&amp;lt;/color&amp;gt;&quot; /&gt;</ph>",
            ),
            "3": mt.InlineTag(
                ph_id="3", tag_type="fmt", desc="⟨actor⟩",
                original_ph_xml="<ph id='3'>&lt;mq:rxt displaytext=&quot;&amp;lt;actor&amp;gt;&quot; /&gt;</ph>",
            ),
            "4": mt.InlineTag(
                ph_id="4", tag_type="/fmt", desc="斜体结束",
                original_ph_xml="<ph id='4'>&lt;mq:rxt displaytext=&quot;&amp;lt;/i&amp;gt;&quot; /&gt;</ph>",
            ),
        }
        out = mt._normalize_bare_tags(
            "本文<color=orange>強調</color>と<i>斜体</i><actor>", tag_map
        )
        self.assertIn("<tag id='1' type='fmt' desc='⟨color=orange⟩'/>", out)
        self.assertIn("<tag id='2' type='/fmt' desc='color结束'/>", out)
        self.assertIn("<tag id='4' type='/fmt' desc='斜体结束'/>", out)
        self.assertIn("<tag id='3' type='fmt' desc='⟨actor⟩'/>", out)
        self.assertNotIn("<color", out)
        self.assertNotIn("</color>", out)
        self.assertNotIn("</i>", out)

    def test_validation_catches_tag_id_mismatch(self):
        """输入期望的标签 id 与输出不一致时，写后校验必须非 0 退出。"""
        tr = {"1": "在主菜单“MATERIA&EQUIPMENT”按"}
        out = self.td / "out3.mqxliff"
        mt.write_translations(self.tree, self.units, tr, output_path=out)
        with self.assertRaises(SystemExit):
            mt._validate_written_targets(
                out, self.units, {"1": "<tag id='9' type='br' desc='换行'/>"}, ["1"]
            )

    def test_validation_catches_literal_ph_text(self):
        """target 里出现字面 <ph ...> 文本（旧降级损坏特征）时，写后校验必须非 0 退出。"""
        corrupt = """<?xml version='1.0' encoding='UTF-8'?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" xmlns:mq="MQXliff" version="1.2">
<file original="t" source-language="ja" target-language="zh-cn" datatype="x-memoq">
<body>
<trans-unit id="1" mq:status="Pretranslated" mq:locked="no">
<source xml:space="preserve">メインメニュー「MATERIA&amp;EQUIPMENT」</source>
<target xml:space="preserve">在主菜单“MATERIA&amp;EQUIPMENT”&lt;ph id="1"&gt;字面标签&lt;/ph&gt;</target>
</trans-unit>
</body>
</file>
</xliff>"""
        bad = self.td / "bad.mqxliff"
        bad.write_text(corrupt, encoding="utf-8")
        units_bad, _ = mt.parse_mqxliff(bad)
        with self.assertRaises(SystemExit):
            mt._validate_written_targets(
                bad, units_bad, {"1": "在主菜单“MATERIA&EQUIPMENT”"}, ["1"]
            )

    def test_import_save_tm_replaces_stale_same_key(self):
        """import --save-tm 用 replace 语义：同键旧译文被提交后的新译文覆盖。"""
        simple = self.td / "simple.mqxliff"
        simple.write_text("""<?xml version='1.0' encoding='UTF-8'?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" xmlns:mq="MQXliff" version="1.2">
<file original="t" source-language="ja" target-language="zh-cn" datatype="x-memoq">
<body>
<trans-unit id="1" mq:status="NotStarted" mq:locked="no">
<source xml:space="preserve">ＭＲ２５０になった</source>
<target xml:space="preserve"></target>
</trans-unit>
</body>
</file>
</xliff>""", encoding="utf-8")
        tm_file = self.td / "tm.json"
        tm_file.write_text(json.dumps({"entries": [
            {"source": "ＭＲ２５０になった", "target": "迈入大师等级250",
             "context": "c1", "file": "simple.mqxliff"},
        ]}, ensure_ascii=False), encoding="utf-8")
        json_file = self.td / "translations.json"
        json_file.write_text(json.dumps({
            "source_file": "simple.mqxliff",
            "entries": [
                {"id": "1", "source": "ＭＲ２５０になった",
                 "target": "大师等级达到250级", "context": "c1"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        out = self.td / "out_save_tm.mqxliff"

        mt.import_from_json(json_file, simple, output_path=out, tm_path=tm_file)

        tm_data = json.loads(tm_file.read_text(encoding="utf-8"))
        self.assertEqual(len(tm_data["entries"]), 1)
        self.assertEqual(tm_data["entries"][0]["target"], "大师等级达到250级")
        self.assertEqual(tm_data["entries"][0]["file"], "simple.mqxliff")

    def test_export_marks_source_locked_entry(self):
        locked = self.td / "locked.mqxliff"
        locked.write_text("""<?xml version='1.0' encoding='UTF-8'?>
<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" xmlns:mq="MQXliff" version="1.2">
<file><body><trans-unit id="1" mq:locked="locked">
<source>翻訳禁止</source><target></target>
</trans-unit></body></file></xliff>""", encoding="utf-8")
        output = mt.export_to_json(locked, output_dir=self.td / "exports")
        data = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(data["entries"][0]["source_locked"])


if __name__ == "__main__":
    unittest.main()
