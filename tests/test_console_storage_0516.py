"""Metadata pagination, periodic retention and bounded thumbnail caching."""
import time
import types
import unittest
from unittest.mock import patch

import test_core as fixtures
from canvas_test import page_api
from canvas_test.storage import StorageError
from PIL import Image


class ConsoleStorage0516Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    async def test_cursor_pagination_does_not_duplicate_after_new_records(self):
        for number in range(7):
            self.store.append_history({"number": number})
        first = self.store.history_page(3)
        self.assertEqual([i["number"] for i in first["items"]], [6, 5, 4])
        self.store.append_history({"number": 7})
        second = self.store.history_page(3, first["next_cursor"])
        third = self.store.history_page(3, second["next_cursor"])
        self.assertEqual([i["number"] for i in second["items"] + third["items"]], [3, 2, 1, 0])
        self.assertFalse(third["has_more"])
        self.assertIsNone(third["next_cursor"])
        for limit, cursor in ((0, None), (101, None), (True, None), (2, -1), (2, True)):
            with self.assertRaises(StorageError):
                self.store.history_page(limit, cursor)

    async def test_periodic_maintenance_prunes_text_only_records_in_batches(self):
        old = time.time() - 35 * 86400
        for number in range(5):
            key = f"text:{number}"
            self.store.reserve_delivery(key)
            self.store.update_delivery(key, {"status": "skipped", "kind": "morning"})
        self.store.reserve_delivery("working")
        self.store.update_delivery("working", {"status": "generating"})
        self.store._db.execute("UPDATE deliveries SET updated_at=?", (old,))
        self.store.reserve_delivery("today")
        self.store.update_delivery("today", {"status": "sent"})
        self.store._db.commit()
        result = self.store.maintenance(100, batch_size=2)
        self.assertEqual(result["deliveries_deleted"], 2)
        for _ in range(3):
            self.store.maintenance(100, batch_size=2)
        self.assertIsNotNone(self.store.delivery("working"))
        self.assertIsNotNone(self.store.delivery("today"))
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 2)

    async def test_old_usage_with_unsettled_reservation_is_preserved(self):
        old = "2020-01-01"
        with self.store._db:
            self.store._db.executemany("INSERT INTO quota_usage VALUES(?,?,1,0)", [("finished", old), ("pending", old)])
            self.store._db.execute("INSERT INTO quota_reservations(user_key,day,at,expires_at,is_admin) VALUES('pending',?,0,1,0)", (old,))
            self.store._db.executemany("INSERT INTO budget_usage VALUES(?,?,1)", [("messages", old), ("photos", old)])
            self.store._db.execute("INSERT INTO budget_reservations VALUES('unfinished','photos',?,1)", (old,))
        result = self.store.maintenance(100)
        self.assertEqual(result["quota_days_deleted"], 1)
        self.assertEqual(result["budget_days_deleted"], 1)
        self.assertEqual(self.store._db.execute("SELECT user_key FROM quota_usage").fetchone()[0], "pending")
        self.assertEqual(self.store._db.execute("SELECT kind FROM budget_usage").fetchone()[0], "photos")

    async def test_maintenance_keeps_assets_referenced_by_pending_or_running_jobs(self):
        asset = self.store.save_asset(fixtures.image_bytes())
        job = self.store.create_job({"status": "generating", "asset": asset.name})
        self.store.append_history({"job_id": job["id"], "image": "/assets/" + asset.name})
        self.store.maintenance(0)
        self.assertTrue(asset.is_file())
        self.assertEqual(self.store.job(job["id"])["status"], "generating")

    async def test_thumbnail_cache_reuses_decode_and_invalidates_changes_and_deletion(self):
        asset = self.store.save_asset(fixtures.image_bytes())
        first = page_api.preview_descriptor(self.plugin, asset.name)
        with patch.object(Image, "open", wraps=Image.open) as opened:
            value = page_api.image_data_url(self.plugin, asset.name, thumbnail=True)
            self.assertEqual(page_api.image_data_url(self.plugin, asset.name, thumbnail=True), value)
            self.assertEqual(opened.call_count, 1)
            Image.new("RGB", (20, 20), "red").save(asset)
            changed = page_api.preview_descriptor(self.plugin, asset.name)
            self.assertNotEqual(first["version"], changed["version"])
            self.assertNotEqual(page_api.image_data_url(self.plugin, asset.name, thumbnail=True), value)
            self.assertEqual(opened.call_count, 2)
        asset.unlink()
        self.assertIsNone(page_api.preview_descriptor(self.plugin, asset.name))
        self.assertNotIn(asset.name, self.plugin._canvas_thumbnail_cache)
        self.assertEqual(page_api.image_data_url(self.plugin, asset.name, thumbnail=True), "")

    async def test_thumbnail_cache_has_entry_and_byte_limits(self):
        with patch.object(page_api, "THUMBNAIL_CACHE_ENTRIES", 2):
            for number in range(4):
                asset = self.store.save_asset(fixtures.image_bytes())
                page_api.image_data_url(self.plugin, asset.name, thumbnail=True)
            self.assertLessEqual(len(self.plugin._canvas_thumbnail_cache), 2)
        with patch.object(page_api, "THUMBNAIL_CACHE_BYTES", 10):
            asset = self.store.save_asset(fixtures.image_bytes())
            self.assertTrue(page_api.image_data_url(self.plugin, asset.name, thumbnail=True))
            self.assertFalse(self.plugin._canvas_thumbnail_cache)


if __name__ == "__main__":
    unittest.main()
