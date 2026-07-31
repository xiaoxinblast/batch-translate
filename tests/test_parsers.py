"""txt / xlsx 解析与写回回归测试。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from parsers import txt_parser, xlsx_parser


class TxtParserTest(unittest.TestCase):
    def test_write_keeps_line_number_ids_with_blank_lines(self):
        """带空行的 txt：id 为行号（空行占号），写回按行号定位、空行原样保留。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "sample.txt"
            src.write_text("こんにちは\n\nさようなら\n", encoding="utf-8")

            data = txt_parser.parse(src)
            self.assertEqual([e["id"] for e in data["entries"]], ["1", "3"])
            data["entries"][0]["target"] = "你好"
            data["entries"][1]["target"] = "再见"  # id 3（行号）

            export = td / "parsed.json"
            export.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            out = txt_parser.write(src, export)

            self.assertEqual(out.read_text(encoding="utf-8"), "你好\n\n再见\n")

    def test_write_plain_array_fallback(self):
        """纯数组输入仍按行序写回。"""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "sample.txt"
            src.write_text("A\nB\n", encoding="utf-8")
            trans = td / "trans.json"
            trans.write_text(
                json.dumps([{"id": "1", "target": "甲"}, {"id": "2", "target": "乙"}], ensure_ascii=False),
                encoding="utf-8",
            )
            out = txt_parser.write(src, trans)
            self.assertEqual(out.read_text(encoding="utf-8"), "甲\n乙\n")


class XlsxParserTest(unittest.TestCase):
    def _make_wb(self, path: Path):
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(row=1, column=1, value="说明行")
        ws.cell(row=2, column=1, value="说明行2")
        ws.cell(row=3, column=1, value="原文")
        ws.cell(row=3, column=2, value="注释")
        ws.cell(row=3, column=3, value="译文")
        ws.cell(row=4, column=1, value="セフィロス")
        ws.cell(row=4, column=2, value="FF7角色")
        ws.cell(row=4, column=3, value="旧译文")
        ws.cell(row=5, column=1, value="ティファ")
        ws.cell(row=5, column=2, value="FF7角色")
        wb.save(str(path))

    def test_parse_skips_header_rows(self):
        """表头行本身不进入条目，id 从表头下一行开始为 1。"""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.xlsx"
            self._make_wb(src)
            data = xlsx_parser.parse(src, source_col="A", target_col="C", header_row=3)
            self.assertEqual([e["id"] for e in data["entries"]], ["1", "2"])
            self.assertEqual(data["entries"][0]["source"], "セフィロス")
            self.assertEqual(data["entries"][0]["_row"], 4)
            self.assertEqual(data["entries"][0]["_target_col"], 2)

    def test_parse_header_row_zero_includes_first_row(self):
        """--header-row 0 表示无表头，数据从第 1 行开始。"""
        with tempfile.TemporaryDirectory() as td:
            import openpyxl

            src = Path(td) / "sample.xlsx"
            wb = openpyxl.Workbook()
            wb.active.cell(row=1, column=1, value="セフィロス")
            wb.save(str(src))
            data = xlsx_parser.parse(src, header_row=0)
            self.assertEqual([e["id"] for e in data["entries"]], ["1"])
            self.assertEqual(data["entries"][0]["_row"], 1)

    def test_write_uses_row_and_target_col(self):
        """写回时按 _row / _target_col 定位，自定义 C 列生效。"""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "sample.xlsx"
            self._make_wb(src)
            data = xlsx_parser.parse(src, source_col="A", target_col="C", header_row=3)
            data["entries"][0]["target"] = "萨菲罗斯"
            data["entries"][1]["target"] = "蒂法"

            export = Path(td) / "parsed.json"
            export.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            out = xlsx_parser.write(src, export)

            import openpyxl

            ws = openpyxl.load_workbook(out).active
            self.assertEqual(ws.cell(row=4, column=3).value, "萨菲罗斯")
            self.assertEqual(ws.cell(row=5, column=3).value, "蒂法")


if __name__ == "__main__":
    unittest.main()
