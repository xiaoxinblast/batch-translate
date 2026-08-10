"""mqxliff 写回（含裸 & 转义）回归测试。"""

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

    def test_bare_actor_tag_normalized(self):
        """裸 <actor> 会被归一化为源文件的 ph 元素并保留。"""
        tr = {"2": "<actor>"}
        out = self.td / "out2.mqxliff"
        mt.write_translations(self.tree, self.units, tr, output_path=out)

        units2, _ = mt.parse_mqxliff(out)
        t2 = next(u for u in units2 if u.id == "2")
        self.assertEqual(t2.target_text, "<tag id='2' type='fmt' desc='⟨actor⟩'/>")

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


if __name__ == "__main__":
    unittest.main()
