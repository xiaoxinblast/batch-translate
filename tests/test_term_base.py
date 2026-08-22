"""Term-base occurrence and conflicting-translation regression tests."""

import sys
import tempfile
import unittest
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from term_base import TermBase


class TermBaseTest(unittest.TestCase):
    def test_repeated_source_term_is_emitted_once_but_conflicts_remain(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "terms.xlsx"
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.append(["原文(ja)", "译文(zh)", "注释"])
            sheet.append(["用語", "术语", "标准译法"])
            sheet.append(["用語", "术语", "标准译法"])
            sheet.append(["用語", "名词", "历史 TM 冲突"])
            workbook.save(path)

            matches = TermBase(path).find_terms("用語と用語")

        self.assertEqual(
            matches,
            [
                {"ja": "用語", "zh": "术语", "note": "标准译法"},
                {"ja": "用語", "zh": "名词", "note": "历史 TM 冲突"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
