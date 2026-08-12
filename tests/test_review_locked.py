"""校对 JSON 的 locked 标记回归测试。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import batch


def _state() -> dict:
    return {
        "document_summary": "",
        "total_batches": 1,
        "context_size": 5,
    }


class ReviewLockedTest(unittest.TestCase):
    def test_locked_flag_preserved_from_translate_batch(self):
        """翻译→校对链路中显式 locked 标记必须保留。"""
        entries = [
            {"id": "1", "source": "A", "target": "已交付", "translated": "已交付", "locked": True},
            {"id": "2", "source": "B", "target": "新译", "translated": "新译", "locked": False},
        ]
        review = batch._build_review_json(entries, _state(), batch_num=1)
        self.assertTrue(review["entries"][0]["locked"])
        self.assertFalse(review["entries"][1]["locked"])

    def test_review_only_does_not_lock_existing_targets(self):
        """next --review 模式下，已有译文是待校对对象，不标记为锁定。"""
        entries = [
            {"id": "1", "source": "A", "target": "已有译文"},
            {"id": "2", "source": "B", "target": ""},
        ]
        review = batch._build_review_json(entries, _state(), batch_num=1, review_only=True)
        self.assertFalse(review["entries"][0]["locked"])
        self.assertFalse(review["entries"][1]["locked"])


if __name__ == "__main__":
    unittest.main()
