"""Offline persistence and atomic ownership tests for morning retries."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage import Storage, StorageError


class MorningRetryStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.storage = self.store()

    def store(self):
        result = Storage(self.directory)
        self.addCleanup(result.close)
        return result

    def prepare(self, *, key="morning:qq:FriendMessage:123:2026-10-13", **patch):
        self.assertTrue(self.storage.reserve_delivery(key))
        self.storage.update_delivery(key, {
            "status": "skipped", "kind": "morning", "umo": "qq:FriendMessage:123",
            "attempts": 1, "retry_at": 100, "retryable": True, "skip_reason": "role_skip",
            "decision": {"decision": "skip", "reply": ""}, **patch,
        })
        return key

    def claim(self, key, **kwargs):
        return self.storage.claim_delivery_retry(key, now=kwargs.pop("now", 100),
            max_attempts=kwargs.pop("max_attempts", 3), allow_role_skip=kwargs.pop("allow_role_skip", True), **kwargs)

    def test_failed_retry_is_atomic_across_connections(self):
        self.assert_atomic_claim("failed")

    def test_role_skip_retry_is_atomic_across_connections(self):
        self.assert_atomic_claim("skipped")

    def assert_atomic_claim(self, status):
        key = self.prepare(status=status)
        other = self.store()
        barrier = threading.Barrier(2)
        def claim(storage):
            barrier.wait(timeout=5)
            return storage.claim_delivery_retry(key, now=100, max_attempts=3, allow_role_skip=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, (self.storage, other)))
        winners = [item for item in results if item is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["status"], "claimed")
        self.assertEqual(winners[0]["attempts"], 2)
        self.assertFalse(winners[0]["retryable"])
        self.assertEqual(self.storage.delivery(key), other.delivery(key))

    def test_retry_is_only_claimed_at_or_after_due_time(self):
        key = self.prepare()
        before = self.storage.delivery(key)
        self.assertIsNone(self.claim(key, now=99.999))
        self.assertEqual(before, self.storage.delivery(key))
        self.assertEqual(self.claim(key, now=100)["attempts"], 2)

    def test_attempt_limit_includes_initial_attempt(self):
        key = self.prepare()
        self.assertEqual(self.claim(key)["attempts"], 2)
        self.storage.update_delivery(key, {"status": "skipped", "retryable": True})
        self.assertEqual(self.claim(key)["attempts"], 3)
        self.storage.update_delivery(key, {"status": "skipped", "retryable": True})
        self.assertIsNone(self.claim(key))
        self.assertEqual(self.storage.delivery(key)["attempts"], 3)

    def test_role_skip_requires_explicit_opt_in_and_all_markers(self):
        cases = [
            ({}, False),
            ({"retryable": False}, True),
            ({"retryable": 1}, True),
            ({"skip_reason": "refuse"}, True),
            ({"skip_reason": "schedule_skip"}, True),
            ({"skip_reason": ""}, True),
        ]
        for index, (patch, allow) in enumerate(cases):
            with self.subTest(patch=patch, allow=allow):
                key = self.prepare(key=f"morning:{index}", **patch)
                before = self.storage.delivery(key)
                self.assertIsNone(self.claim(key, allow_role_skip=allow))
                self.assertEqual(before, self.storage.delivery(key))

    def test_all_other_delivery_statuses_remain_terminal(self):
        for status in ("claimed", "decided", "generating", "sending", "sent", "uncertain", "delivery_failed"):
            with self.subTest(status=status):
                key = self.prepare(key=f"morning:{status}", status=status)
                self.assertIsNone(self.claim(key))
                self.assertEqual(self.storage.delivery(key)["attempts"], 1)

    def test_failed_delivery_can_retry_without_role_skip_opt_in(self):
        key = self.prepare(status="failed", retryable=False, skip_reason="")
        self.assertEqual(self.claim(key, allow_role_skip=False)["attempts"], 2)

    def test_claim_preserves_fields_returns_detached_snapshot_and_survives_restart(self):
        key = self.prepare(extra={"topic": "昨晚的聊天"})
        before = self.storage.delivery(key)
        claimed = self.claim(key)
        for field in ("key", "at", "kind", "umo", "decision", "extra", "retry_at", "skip_reason"):
            self.assertEqual(claimed[field], before[field])
        claimed["extra"]["topic"] = "changed"
        self.storage.close()
        restarted = self.store()
        persisted = restarted.delivery(key)
        self.assertEqual(persisted["extra"], before["extra"])
        self.assertEqual(persisted["attempts"], 2)
        self.assertFalse(persisted["retryable"])
        self.assertIsNone(restarted.claim_delivery_retry(key, now=1000, max_attempts=3, allow_role_skip=True))

    def test_due_role_skip_survives_restart_before_claim(self):
        key = self.prepare()
        self.storage.close()
        restarted = self.store()
        self.assertEqual(restarted.claim_delivery_retry(key, now=100, max_attempts=3, allow_role_skip=True)["attempts"], 2)

    def test_missing_record_or_unusable_retry_metadata_is_not_claimed(self):
        self.assertIsNone(self.claim("morning:missing"))
        cases = [{"retry_at": None}, {"retry_at": "100"}, {"attempts": "1"}, {"attempts": True}, {"attempts": -1}]
        for index, patch in enumerate(cases):
            with self.subTest(patch=patch):
                key = self.prepare(key=f"morning:invalid:{index}", **patch)
                self.assertIsNone(self.claim(key))
        key = "morning:legacy"
        self.storage.reserve_delivery(key)
        self.storage.update_delivery(key, {"status": "failed"})
        self.assertIsNone(self.claim(key))
        self.storage.update_delivery(key, {"retry_at": 100})
        self.assertEqual(self.claim(key)["attempts"], 2)

    def test_new_retry_defaults_merge_into_existing_saved_settings(self):
        self.storage.settings["good_morning"] = {"enabled": True, "start": "08:00", "end": "09:00", "timezone": "UTC"}
        self.storage.save_settings()
        self.storage.close()
        reopened = self.store()
        self.assertEqual(reopened.settings["good_morning"], {
            "enabled": True, "start": "08:00", "end": "09:00", "timezone": "UTC",
            "max_attempts": 3, "retry_interval_sec": 900,
        })

    def test_invalid_claim_arguments_are_rejected(self):
        key = self.prepare()
        for arguments in ({"now": float("nan")}, {"now": True}, {"max_attempts": 0}, {"max_attempts": True}, {"max_attempts": 3.1}):
            with self.subTest(arguments=arguments), self.assertRaises(StorageError):
                self.claim(key, **arguments)
        self.assertEqual(self.storage.delivery(key)["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
