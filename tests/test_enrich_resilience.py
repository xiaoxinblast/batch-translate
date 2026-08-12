"""_enrich_working_json 逐条容错回归测试：单条坏数据不得中断整体增强。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import batch


class EnrichResilienceTest(unittest.TestCase):
    def test_bad_source_does_not_abort_enrich(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tm_file = tmp / "tm.json"
            tm_file.write_text(json.dumps({"entries": [
                {"source": "ABC", "target": "译文ABC", "context": "c1", "file": "f1"},
                {"source": "DEF", "target": "译文DEF", "context": "c2", "file": "f2"},
            ]}, ensure_ascii=False), encoding="utf-8")

            import openpyxl
            tb_file = tmp / "terms.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["原文(ja)", "译文(zh)", "注释"])
            ws.append(["ABC", "术语ABC", "n"])
            wb.save(tb_file)
            wb.close()

            sg_file = tmp / "style_guide.txt"
            sg_file.write_text("规则", encoding="utf-8")

            work_json = tmp / "working.json"
            entries = [
                {"id": "1", "source": "ABC", "context": "c1"},
                {"id": "2", "source": None, "context": "c2"},  # 坏数据：source 非字符串
                {"id": "3", "source": "DEF", "context": "c2"},
            ]
            work_json.write_text(json.dumps({"entries": entries}, ensure_ascii=False),
                                 encoding="utf-8")

            state = {
                "terms_path": str(tb_file),
                "tm_path": str(tm_file),
                "style_guide_path": str(sg_file),
            }
            # 不应抛异常，坏条目后的条目也必须被增强
            batch._enrich_working_json(work_json, state)

            data = json.loads(work_json.read_text(encoding="utf-8"))
            by_id = {e["id"]: e for e in data["entries"]}
            self.assertIn("tm_matches", by_id["1"])
            self.assertIn("terms", by_id["1"])
            self.assertNotIn("tm_matches", by_id["2"])
            self.assertNotIn("terms", by_id["2"])
            self.assertIn("tm_matches", by_id["3"])
            self.assertEqual(by_id["3"]["tm_matches"][0]["target"], "译文DEF")

    def test_missing_resources_do_not_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            work_json = tmp / "working.json"
            work_json.write_text(json.dumps({"entries": [
                {"id": "1", "source": "ABC", "context": "c1"},
            ]}, ensure_ascii=False), encoding="utf-8")
            state = {
                "terms_path": str(tmp / "missing_terms.xlsx"),
                "tm_path": str(tmp / "missing_tm.json"),
                "style_guide_path": str(tmp / "missing_guide.txt"),
            }
            batch._enrich_working_json(work_json, state)  # 不应抛异常
            data = json.loads(work_json.read_text(encoding="utf-8"))
            self.assertNotIn("tm_matches", data["entries"][0])


if __name__ == "__main__":
    unittest.main()
