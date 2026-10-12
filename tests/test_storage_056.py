"""Offline regressions for retry ownership and reservation settlement."""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from moderation import Moderation
from storage import DEFAULT_SETTINGS, Storage, StorageConflictError


class RetryAndQuota056Tests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.storage = self.store()

    def store(self):
        result = Storage(self.directory)
        self.addCleanup(result.close)
        return result

    def prepare_retry(self, *, conditional=False):
        session = self.storage.session("conversation", "default")
        old = self.storage.create_job({"status": "failed", "session_key": "conversation", "persona_id": "default", "umo": "qq:friend:123", "confirmed_request_id": "request-1" if conditional else ""})
        if conditional:
            session["pending"] = {"request_id": "request-1", "session_key": "conversation", "persona_id": "default", "request_kind": "photo", "execution_job_id": old["id"], "expires_at": time.time() + 1800, "requirements": {"outfit": "sailor uniform"}}
            session = self.storage.save_session("conversation", session)
        candidates = [self.storage.create_job({"status": "queued", "session_key": "conversation", "persona_id": "default", "umo": "qq:friend:123"}) for _ in range(2)]
        return old, candidates, session

    def race_claim(self, old, candidates):
        other = self.store()
        barrier = threading.Barrier(2)
        def claim(pair):
            storage, candidate = pair
            barrier.wait(timeout=5)
            return storage.claim_job_retry(old["id"], candidate["id"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(claim, zip((self.storage, other), candidates)))

    def test_direct_photo_retry_is_atomic_across_connections(self):
        old, candidates, session = self.prepare_retry()
        results = self.race_claim(old, candidates)
        self.assertEqual([False, True], sorted(result["claimed"] for result in results))
        self.assertEqual(results[0]["job_id"], results[1]["job_id"])
        winner = self.storage.job(old["id"])["retry_job_id"]
        self.assertEqual(winner, results[0]["job_id"])
        self.assertEqual(old["id"], self.storage.job(winner)["retry_of"])
        self.assertEqual(session["revision"], self.storage.read_session("conversation")["revision"])

    def test_conditional_retry_rebinds_consent_in_the_same_transaction(self):
        old, candidates, session = self.prepare_retry(conditional=True)
        results = self.race_claim(old, candidates)
        self.assertEqual([False, True], sorted(result["claimed"] for result in results))
        current = self.storage.read_session("conversation")
        self.assertEqual(session["revision"] + 1, current["revision"])
        self.assertEqual(results[0]["job_id"], current["pending"]["execution_job_id"])
        self.assertEqual("request-1", self.storage.job(results[0]["job_id"])["confirmed_request_id"])
        for result in results:
            self.assertEqual(current, result["session"])

    def test_withdrawn_conditions_cannot_be_reclaimed(self):
        old, candidates, session = self.prepare_retry(conditional=True)
        session["pending"] = None
        self.storage.save_session("conversation", session)
        with self.assertRaises(StorageConflictError):
            self.storage.claim_job_retry(old["id"], candidates[0]["id"])
        self.assertNotIn("retry_job_id", self.storage.job(old["id"]))
        self.assertNotIn("retry_of", self.storage.job(candidates[0]["id"]))

    def test_revised_conditions_reject_old_retry_without_partial_rebinding(self):
        old, candidates, session = self.prepare_retry(conditional=True)
        session["pending"]["request_id"] = "request-2"
        current = self.storage.save_session("conversation", session)
        with self.assertRaises(StorageConflictError):
            self.storage.claim_job_retry(old["id"], candidates[0]["id"])
        self.assertEqual(current, self.storage.read_session("conversation"))
        self.assertNotIn("retry_job_id", self.storage.job(old["id"]))

    def test_changed_role_and_new_pending_reject_direct_retry(self):
        old, candidates, session = self.prepare_retry()
        session["pending"] = {"request_id": "new-request"}
        self.storage.save_session("conversation", session)
        with self.assertRaises(StorageConflictError):
            self.storage.claim_job_retry(old["id"], candidates[0]["id"])
        self.storage.upsert_persona({"id": "other", "name": "Other"})
        self.storage.session("conversation", "other")
        with self.assertRaises(StorageConflictError):
            self.storage.claim_job_retry(old["id"], candidates[1]["id"])

    def test_generated_retry_is_reused_even_when_delivery_failed(self):
        old, candidates, _session = self.prepare_retry()
        first = self.storage.claim_job_retry(old["id"], candidates[0]["id"])
        self.storage.update_job(first["job_id"], {"status": "delivery_failed", "asset": "existing-photo.png"})
        second = self.storage.claim_job_retry(old["id"], candidates[1]["id"])
        self.assertFalse(second["claimed"])
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual("existing-photo.png", second["job"]["asset"])

    def test_failed_direct_retry_can_be_explicitly_retried_again(self):
        old, candidates, _session = self.prepare_retry()
        first = self.storage.claim_job_retry(old["id"], candidates[0]["id"])
        self.storage.update_job(first["job_id"], {"status": "failed"})
        second = self.storage.claim_job_retry(old["id"], candidates[1]["id"])
        self.assertTrue(second["claimed"])
        self.assertEqual(candidates[1]["id"], self.storage.job(old["id"])["retry_job_id"])

    def test_finished_conditional_retry_is_reused_after_pending_is_cleared(self):
        old, candidates, _session = self.prepare_retry(conditional=True)
        first = self.storage.claim_job_retry(old["id"], candidates[0]["id"])
        self.storage.update_job(first["job_id"], {"status": "sent"})
        session = self.storage.read_session("conversation")
        session["pending"] = None
        self.storage.save_session("conversation", session)
        second = self.storage.claim_job_retry(old["id"], candidates[1]["id"])
        self.assertFalse(second["claimed"])
        self.assertEqual(first["job_id"], second["job_id"])

    def test_unfinished_conditional_retry_is_not_reused_after_withdrawal(self):
        old, candidates, _session = self.prepare_retry(conditional=True)
        self.storage.claim_job_retry(old["id"], candidates[0]["id"])
        session = self.storage.read_session("conversation")
        session["pending"] = None
        self.storage.save_session("conversation", session)
        with self.assertRaises(StorageConflictError):
            self.storage.claim_job_retry(old["id"], candidates[1]["id"])

    def test_cancelled_retry_does_not_reopen_original_authorization(self):
        old, candidates, _session = self.prepare_retry()
        first = self.storage.claim_job_retry(old["id"], candidates[0]["id"])
        self.storage.update_job(first["job_id"], {"status": "cancelled", "cancel_requested": True})
        with self.assertRaises(StorageConflictError):
            self.storage.claim_job_retry(old["id"], candidates[1]["id"])

    def test_delivery_failure_is_not_a_generation_retry(self):
        old, candidates, _session = self.prepare_retry()
        self.storage.update_job(old["id"], {"status": "delivery_failed"})
        with self.assertRaises(StorageConflictError):
            self.storage.claim_job_retry(old["id"], candidates[0]["id"])
        self.storage.reserve_delivery("delivery-1")
        self.assertEqual("delivery_failed", self.storage.update_delivery("delivery-1", {"status": "delivery_failed"})["status"])

    def test_cross_day_out_of_order_completion_uses_each_token_day(self):
        limits = {"daily_limit": 1, "min_interval_sec": 0, "max_concurrency": 2, "timezone": "Asia/Hong_Kong"}
        before = datetime(2026, 10, 11, 15, 59, tzinfo=timezone.utc).timestamp()
        after = before + 120
        with patch("storage.time.time", return_value=before):
            allowed, _reason, old_token = self.storage.reserve_quota_token("u", limits)
            self.assertTrue(allowed)
        with patch("storage.time.time", return_value=after):
            allowed, _reason, new_token = self.storage.reserve_quota_token("u", limits)
            self.assertTrue(allowed)
            self.storage.finish_quota_token(new_token, success=True)
            self.storage.finish_quota_token(old_token, success=False)
            self.assertFalse(self.storage.reserve_quota_token("u", limits)[0])
        rows = {row["day"]: row["successes"] for row in self.storage._db.execute("SELECT day,successes FROM quota_usage WHERE user_key='u'")}
        self.assertEqual({"2026-10-11": 0, "2026-10-12": 1}, rows)

    def test_tokens_settle_independently_and_only_once_across_connections(self):
        limits = {"daily_limit": 2, "min_interval_sec": 0, "max_concurrency": 2}
        first = self.storage.reserve_quota_token("u", limits)[2]
        second = self.storage.reserve_quota_token("u", limits)[2]
        other = self.store()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda store: store.finish_quota_token(second, success=True), (self.storage, other)))
        pending = self.storage._db.execute("SELECT id FROM quota_reservations").fetchall()
        self.assertEqual([int(first)], [row[0] for row in pending])
        self.assertEqual(1, self.storage._db.execute("SELECT successes FROM quota_usage WHERE user_key='u'").fetchone()[0])
        self.storage.finish_quota_token(first, success=False)
        self.assertTrue(self.storage.reserve_quota_token("u", limits)[0])

    def test_admin_reservations_do_not_occupy_normal_limit_or_concurrency(self):
        limits = {"daily_limit": 1, "min_interval_sec": 0, "max_concurrency": 1}
        admin = self.storage.reserve_quota_token("same-user", limits, is_admin=True)[2]
        allowed, _reason, normal = self.storage.reserve_quota_token("same-user", limits)
        self.assertTrue(allowed)
        self.assertFalse(self.storage.reserve_quota_token("same-user", limits)[0])
        self.storage.finish_quota_token(normal, success=True)
        self.storage.finish_quota_token(admin, success=True)
        self.assertEqual(1, self.storage._db.execute("SELECT successes FROM quota_usage WHERE user_key='same-user'").fetchone()[0])

    def test_admin_requests_do_not_change_normal_user_cooldown(self):
        limits = {"daily_limit": 0, "min_interval_sec": 60, "max_concurrency": 1}
        with patch("storage.time.time", return_value=1000):
            token = self.storage.reserve_quota_token("u", limits)[2]
            self.storage.finish_quota_token(token)
        with patch("storage.time.time", return_value=1100):
            admin = self.storage.reserve_quota_token("u", limits, is_admin=True)[2]
        with patch("storage.time.time", return_value=1101):
            self.assertTrue(self.storage.reserve_quota_token("u", limits)[0])
            self.storage.finish_quota_token(admin, success=True)

    def test_moderation_reservation_checks_content_and_settles_exact_token(self):
        moderation = Moderation({"moderation": {"daily_limit": 2, "min_interval_sec": 0, "max_concurrency": 2}}, self.storage)
        self.assertEqual((False, "该请求不允许生成", None), moderation.reserve("u", "儿童裸体"))
        first = moderation.reserve("u", "portrait")[2]
        second = moderation.reserve("u", "portrait")[2]
        moderation.finish("u", success=True, token=second)
        moderation.finish("u", success=False, token=first)
        self.assertEqual(1, self.storage._db.execute("SELECT successes FROM quota_usage WHERE user_key='u'").fetchone()[0])
        self.assertFalse(self.storage._db.execute("SELECT 1 FROM quota_reservations").fetchone())

    def test_standalone_moderation_keeps_admin_and_normal_tokens_separate(self):
        moderation = Moderation({"moderation": {"daily_limit": 1, "min_interval_sec": 0, "max_concurrency": 1}})
        admin = moderation.reserve("u", "portrait", is_admin=True)[2]
        allowed, _reason, normal = moderation.reserve("u", "portrait")
        self.assertTrue(allowed)
        moderation.finish("u", success=True, token=admin)
        self.assertFalse(moderation.reserve("u", "portrait")[0])
        moderation.finish("u", success=False, token=normal)
        self.assertTrue(moderation.reserve("u", "portrait")[0])

    def test_default_target_concurrency_is_bounded(self):
        self.assertEqual(3, DEFAULT_SETTINGS["active"]["max_concurrent_targets"])


if __name__ == "__main__":
    unittest.main()
