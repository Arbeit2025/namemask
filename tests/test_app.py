import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["NAMEMASK_DB"] = _tmp.name

import app as app_module  # noqa: E402

SAMPLE = (
    "連絡先は tanaka@example.jp、電話は 090-1234-5678、"
    "住所は 〒100-0001 です。サイトは https://example.com/info を見てください。"
)


class NameMaskTest(unittest.TestCase):
    def setUp(self):
        app_module.init_db()
        self.client = app_module.app.test_client()
        with app_module.app.app_context():
            conn = app_module.get_db()
            conn.execute("DELETE FROM maps")
            conn.commit()

    def test_healthz(self):
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "ok")

    def test_mask_patterns(self):
        masked, entries = app_module.mask_text(SAMPLE, [])
        self.assertNotIn("tanaka@example.jp", masked)
        self.assertNotIn("090-1234-5678", masked)
        self.assertNotIn("100-0001", masked)
        self.assertNotIn("https://example.com/info", masked)
        self.assertIn("【メール1】", masked)
        self.assertIn("【電話1】", masked)
        self.assertIn("【郵便番号1】", masked)
        self.assertIn("【URL1】", masked)
        self.assertEqual(len(entries), 4)

    def test_roundtrip(self):
        masked, entries = app_module.mask_text(SAMPLE, [])
        restored = app_module.restore_text(masked, entries)
        self.assertEqual(restored, SAMPLE)

    def test_same_original_same_alias(self):
        text = "a@example.com と a@example.com"
        masked, entries = app_module.mask_text(text, [])
        self.assertEqual(masked, "【メール1】 と 【メール1】")
        self.assertEqual(len(entries), 1)

    def test_dictionary_first(self):
        text = "株式会社ほげの田中です。連絡は tanaka@example.jp へ。"
        dictionary = [("株式会社ほげ", "【会社A】"), ("田中", "【人物A】")]
        masked, entries = app_module.mask_text(text, dictionary)
        self.assertNotIn("株式会社ほげ", masked)
        self.assertNotIn("田中", masked)
        self.assertIn("【会社A】", masked)
        restored = app_module.restore_text(masked, entries)
        self.assertEqual(restored, text)

    def test_fixed_phone(self):
        masked, _ = app_module.mask_text("固定電話は 03-1234-5678 です", [])
        self.assertIn("【電話1】", masked)

    def test_save_and_restore_via_pages(self):
        res = self.client.post(
            "/mask",
            data={"text": SAMPLE, "map_name": "テスト対応表", "orig": [""], "alias": [""]},
        )
        body = res.get_data(as_text=True)
        self.assertIn("【メール1】", body)
        with app_module.app.app_context():
            conn = app_module.get_db()
            row = conn.execute("SELECT id FROM maps WHERE name = 'テスト対応表'").fetchone()
            self.assertIsNotNone(row)
            map_id = row["id"]
        masked, _ = app_module.mask_text(SAMPLE, [])
        res = self.client.post(f"/restore/{map_id}", data={"text": masked})
        self.assertIn(SAMPLE, res.get_data(as_text=True))

    def test_index_ok(self):
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
