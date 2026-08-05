"""rebuild_tm.py 的列检测回归测试：表头跳过、--header-row、--clean-source。"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import openpyxl
import rebuild_tm


def _make_xlsx(path: Path, rows: list[list]) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


class RebuildTmTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_explicit_columns_skip_header(self):
        """显式列时自动识别表头并跳过，不再混入表头条目。"""
        p = _make_xlsx(self.dir / "a.xlsx", [
            ["[ja-JP] Subtitle\nAnnotation/Comment/Content",
             "[zh-CN] Subtitle\nAnnotation/Comment/Content"],
            ["日本語のテキスト", "中文译文"],
        ])
        entries = rebuild_tm.extract_xlsx(p, source_col="A", target_col="B")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "日本語のテキスト")
        self.assertEqual(entries[0]["target"], "中文译文")

    def test_header_row_zero_keeps_old_behavior(self):
        """--header-row 0 时把表头行也当作数据（兼容旧行为）。"""
        p = _make_xlsx(self.dir / "a.xlsx", [
            ["[ja-JP] Subtitle", "[zh-CN] Subtitle"],
            ["日本語のテキスト", "中文译文"],
        ])
        entries = rebuild_tm.extract_xlsx(
            p, source_col="A", target_col="B", header_row=0
        )
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["source"], "[ja-JP] Subtitle")

    def test_clean_source_picks_column_without_comments(self):
        """--clean-source 自动避开 # / // 注释前缀的列。"""
        p = _make_xlsx(self.dir / "a.xlsx", [
            ["[ja-JP] Subtitle", "[ja-JP] Subtitle", "[zh-CN] Subtitle"],
            ["# 日本語Fix\n日本語１", "日本語１", "中文１"],
            ["// コメント\n日本語２", "日本語２", "中文２"],
        ])
        entries = rebuild_tm.extract_xlsx(p, target_col="C", clean_source=True)
        self.assertEqual([e["source"] for e in entries], ["日本語１", "日本語２"])

    def test_clean_source_rejects_datetime_column(self):
        """--clean-source 避开日期时间列（如 Created At）。"""
        p = _make_xlsx(self.dir / "a.xlsx", [
            ["[ja-JP] Subtitle", "[ja-JP] Subtitle", "[zh-CN] Subtitle"],
            ["日本語１", "2025-08-22 04:15:45", "中文１"],
            ["日本語２", "2026-01-01 00:00:00", "中文２"],
        ])
        entries = rebuild_tm.extract_xlsx(p, target_col="C", clean_source=True)
        self.assertEqual([e["source"] for e in entries], ["日本語１", "日本語２"])

    def test_clean_source_tie_keeps_leftmost(self):
        """多个候选列同样干净时取最左列。"""
        p = _make_xlsx(self.dir / "a.xlsx", [
            ["[ja-JP] Subtitle", "[ja-JP] Subtitle", "[zh-CN] Subtitle"],
            ["日本語１", "日本語１", "中文１"],
            ["日本語２", "日本語２", "中文２"],
        ])
        entries = rebuild_tm.extract_xlsx(p, target_col="C", clean_source=True)
        self.assertEqual(entries[0]["source"], "日本語１")


if __name__ == "__main__":
    unittest.main()
