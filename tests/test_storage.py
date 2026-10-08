"""Persistent storage contracts; all fixtures live in temporary directories."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage import SCHEMA_VERSION, Storage, StorageConflictError, StorageError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def store(self, directory=None):
        result = Storage(directory or self.directory)
        self.addCleanup(result.close)
        return result

    def write_json(self, name, value):
        (self.directory / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def connection(self):
        result = sqlite3.connect(self.directory / "persona_canvas.sqlite3")
        self.addCleanup(result.close)
        return result

    def test_facades_persist_in_database_and_new_persona_does_not_replace_default(self):
        first = self.store()
        persona = first.upsert_persona({"name": "测试", "state": {"outfit": "coat"}})
        self.assertNotEqual("default", persona["id"])
        first.settings["current_persona"] = persona["id"]
        first.targets["items"] = [{"umo": "qq:friend:123", "enabled": True}]
        first.runtime["morning"] = {"2026-10-07": True}
        first.save_all()
        first.close()
        second = self.store()
        self.assertEqual("测试", second.persona()["name"])
        self.assertEqual("coat", second.persona()["state"]["outfit"])
        self.assertEqual("默认人设", second.persona("default")["name"])
        self.assertEqual("qq:friend:123", second.targets["items"][0]["umo"])
        self.assertTrue(second.runtime["morning"]["2026-10-07"])
        self.assertTrue(second.db_path.exists())
        self.assertFalse(second.personas_path.exists())

    def test_partial_persona_updates_preserve_fixed_identity(self):
        store = self.store()
        persona = store.upsert_persona({"id": "p", "positive_prompt": "silver hair", "state": {"outfit": "coat"}})
        updated = store.upsert_persona({"id": "p", "name": "新名字"})
        self.assertEqual("silver hair", updated["positive_prompt"])
        self.assertEqual("coat", updated["state"]["outfit"])
        self.assertEqual(persona["created_at"], updated["created_at"])

    def test_sessions_are_isolated_revision_checked_and_restart_durable(self):
        first = self.store()
        first.upsert_persona({"id": "p", "state": {"outfit": "coat"}})
        alice = first.session("alice", "p")
        bob = first.session("bob", "p")
        alice["state"]["outfit"] = "dress"
        alice["pending"] = {"description": "换装", "expires_at": 9999999999}
        saved = first.save_session("alice", alice)
        self.assertEqual(1, saved["revision"])
        self.assertEqual("coat", first.session("bob")["state"]["outfit"])
        self.assertEqual("coat", first.persona("p")["state"]["outfit"])
        with self.assertRaises(StorageConflictError):
            first.save_session("alice", alice)
        first.close()
        second = self.store()
        self.assertEqual("dress", second.session("alice")["state"]["outfit"])
        self.assertEqual("换装", second.session("alice")["pending"]["description"])
        self.assertEqual("coat", bob["state"]["outfit"])

    def test_deleting_persona_rebinds_sessions_and_selected_persona(self):
        store = self.store()
        store.upsert_persona({"id": "p", "state": {"outfit": "coat"}})
        store.settings["current_persona"] = "p"
        store.save_settings()
        original_settings = store.settings
        before = store.session("alice", "p")
        self.assertTrue(store.delete_persona("p"))
        self.assertEqual("default", store.session("alice")["persona_id"])
        self.assertEqual(before["revision"] + 1, store.session("alice")["revision"])
        self.assertEqual("default", store.settings["current_persona"])
        self.assertIs(original_settings, store.settings)
        self.assertFalse(store.delete_persona("p"))
        with self.assertRaises(StorageError):
            store.delete_persona("default")

    def test_valid_json_migration_is_backed_up_once_and_preserves_source(self):
        self.write_json("personas.json", {"version": 2, "items": [{"id": "p", "name": "旧人设", "reference_path": "old/path/reference.png"}]})
        self.write_json("settings.json", {"current_persona": "p", "auto_route": False, "providers": {"default": {"api_key": "fixture-secret"}}})
        self.write_json("targets.json", {"items": [{"umo": "qq:friend:123"}]})
        self.write_json("runtime.json", {"morning": {"claimed": True}})
        (self.directory / "history.jsonl").write_text('{"at":12,"ok":true}\n', encoding="utf-8")
        (self.directory / "assets").mkdir()
        (self.directory / "assets" / "reference.png").write_bytes(b"reference-fixture")
        original = (self.directory / "personas.json").read_bytes()
        first = self.store()
        self.assertEqual("reference.png", first.persona("p")["reference_asset"])
        self.assertFalse(first.settings["integration"]["enabled"])
        self.assertEqual("native_tools", first.settings["integration"]["mode"])
        self.assertTrue(first.recent_history()[0]["ok"])
        self.assertEqual(original, (self.directory / "personas.json").read_bytes())
        backups = list((self.directory / "migration_backups").glob("legacy-json-*"))
        self.assertEqual(1, len(backups))
        self.assertEqual(original, (backups[0] / "personas.json").read_bytes())
        first.upsert_persona({"id": "p", "name": "新人设"})
        first.close()
        second = self.store()
        self.assertEqual("新人设", second.persona("p")["name"])
        self.assertEqual(1, len(second.recent_history()))
        self.assertEqual(1, len(list((self.directory / "migration_backups").glob("legacy-json-*"))))

    def test_corrupt_json_fails_without_committing_defaults_and_can_be_retried(self):
        path = self.directory / "personas.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(StorageError, "数据损坏"):
            Storage(self.directory)
        self.assertEqual("{broken", path.read_text(encoding="utf-8"))
        with self.connection() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        backups = list((self.directory / "migration_backups").glob("legacy-json-*"))
        self.assertEqual("{broken", (backups[0] / "personas.json").read_text(encoding="utf-8"))
        self.write_json("personas.json", {"items": [{"id": "p", "name": "恢复"}]})
        self.assertEqual("恢复", self.store().persona("p")["name"])

    def test_structurally_invalid_json_also_fails_without_overwrite(self):
        self.write_json("personas.json", {"items": "invalid"})
        original = (self.directory / "personas.json").read_bytes()
        with self.assertRaises(StorageError):
            Storage(self.directory)
        self.assertEqual(original, (self.directory / "personas.json").read_bytes())

    def test_sqlite_upgrade_creates_a_backup_and_newer_schema_is_rejected(self):
        first = self.store()
        first.close()
        with self.connection() as db:
            db.execute("PRAGMA user_version=2")
        second = self.store()
        self.assertEqual(1, len(list((self.directory / "migration_backups").glob("sqlite-v2-*"))))
        second.close()
        with self.connection() as db:
            self.assertEqual(SCHEMA_VERSION, db.execute("PRAGMA user_version").fetchone()[0])
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(StorageError, "更新版本"):
            Storage(self.directory)

    def test_explicit_directory_never_looks_for_live_legacy_plugin(self):
        with patch("storage._legacy_data_dir", side_effect=AssertionError("must not scan live data")):
            self.store()

    def test_jobs_and_delivery_claims_survive_restart_without_resending(self):
        first = self.store()
        job = first.create_job({"umo": "alice"})
        first.update_job(job["id"], {"status": "sending", "caption": "hello"})
        self.assertTrue(first.reserve_delivery("morning:alice:2026-10-07"))
        first.update_delivery("morning:alice:2026-10-07", {"status": "sending", "job_id": job["id"]})
        first.close()
        second = self.store()
        self.assertEqual("sending", second.job(job["id"])["status"])
        self.assertEqual("sending", second.delivery("morning:alice:2026-10-07")["status"])
        self.assertFalse(second.reserve_delivery("morning:alice:2026-10-07"))
        self.assertIsNone(second.job("missing"))
        with self.assertRaises(StorageError):
            second.update_job(job["id"], {"status": "invalid"})

    def test_delivery_claim_is_atomic_across_connections(self):
        first, second = self.store(), self.store()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda store: store.reserve_delivery("same"), (first, second)))
        self.assertEqual([False, True], sorted(results))

    def test_quota_counts_success_and_pending_refunds_failure_and_survives_restart(self):
        limits = {"daily_limit": 1, "min_interval_sec": 0, "max_concurrency": 2}
        first = self.store()
        self.assertTrue(first.reserve_quota("u", limits)[0])
        self.assertFalse(first.reserve_quota("u", limits)[0])
        first.finish_quota("u", success=False)
        self.assertTrue(first.reserve_quota("u", limits)[0])
        first.finish_quota("u", success=True)
        first.close()
        second = self.store()
        self.assertFalse(second.reserve_quota("u", limits)[0])
        self.assertTrue(second.reserve_quota("admin", limits, is_admin=True)[0])
        second.finish_quota("admin", success=True)
        self.assertTrue(second.reserve_quota("admin", limits)[0])

    def test_quota_daily_boundary_uses_configured_timezone(self):
        store = self.store()
        limits = {"daily_limit": 1, "min_interval_sec": 0, "max_concurrency": 1, "timezone": "Asia/Hong_Kong"}
        before = datetime(2026, 10, 7, 15, 59, tzinfo=timezone.utc).timestamp()
        after = datetime(2026, 10, 7, 16, 1, tzinfo=timezone.utc).timestamp()
        with patch("storage.time.time", return_value=before):
            self.assertTrue(store.reserve_quota("u", limits)[0])
            store.finish_quota("u", success=True)
            self.assertFalse(store.reserve_quota("u", limits)[0])
        with patch("storage.time.time", return_value=after):
            self.assertTrue(store.reserve_quota("u", limits)[0])

    def test_quota_reservation_is_atomic_across_connections(self):
        first, second = self.store(), self.store()
        limits = {"daily_limit": 1, "min_interval_sec": 0, "max_concurrency": 2}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda store: store.reserve_quota("u", limits)[0], (first, second)))
        self.assertEqual([False, True], sorted(results))

    def test_admin_bypasses_per_user_limits_but_reservations_can_be_finished(self):
        store = self.store()
        limits = {"daily_limit": 1, "min_interval_sec": 60, "max_concurrency": 1}
        self.assertTrue(store.reserve_quota("admin", limits, is_admin=True)[0])
        self.assertTrue(store.reserve_quota("admin", limits, is_admin=True)[0])
        store.finish_quota("admin", success=True)
        store.finish_quota("admin", success=True)
        with patch("storage.time.time", return_value=10_000_000_000):
            self.assertTrue(store.reserve_quota("admin", limits)[0])

    def test_quota_stale_reservations_expire_but_do_not_count_as_success(self):
        store = self.store()
        limits = {"daily_limit": 1, "min_interval_sec": 0, "max_concurrency": 1, "reservation_timeout_sec": 60}
        with patch("storage.time.time", return_value=1000):
            self.assertTrue(store.reserve_quota("u", limits)[0])
        with patch("storage.time.time", return_value=1200):
            self.assertTrue(store.reserve_quota("u", limits)[0])

    def test_secrets_are_recursively_redacted_and_errors_scrubbed(self):
        store = self.store()
        store.settings["providers"]["default"]["api_key"] = "fixture-api-secret"
        store.settings["providers"]["default"]["extra_body"] = {"headers": {"Authorization": "Bearer fixture-nested-secret"}, "nested": [{"auth_token": "fixture-token-secret"}]}
        store.save_settings()
        safe = store.safe_settings()
        provider = safe["providers"]["default"]
        self.assertNotIn("api_key", provider)
        self.assertTrue(provider["has_api_key"])
        self.assertEqual("Authorization", provider["auth_header"])
        self.assertTrue(provider["extra_body"]["headers"]["has_authorization"])
        store.append_history({"ok": False, "error": "request with fixture-api-secret failed", "api_key": "fixture-api-secret"})
        exported = json.dumps(store.export())
        self.assertNotIn("fixture-api-secret", exported)
        self.assertNotIn("fixture-nested-secret", exported)
        self.assertNotIn("fixture-token-secret", exported)
        self.assertIn("[REDACTED]", store.recent_history()[0]["error"])
        self.assertEqual("fixture-api-secret", store.export(include_secrets=True)["settings"]["providers"]["default"]["api_key"])

    def test_asset_paths_are_validated_and_history_cleanup_preserves_references(self):
        store = self.store()
        referenced = store.save_asset(b"referenced-fixture")
        obsolete = store.save_asset(b"obsolete-fixture", "jpg")
        recent = store.save_asset(b"recent-fixture")
        for name in ("../settings.json", "sub/image.png", "C:\\secret.png", "..\\secret.png", ".hidden.png"):
            with self.assertRaises(StorageError):
                store.asset(name)
        with self.assertRaises(StorageError):
            store.save_asset(b"fixture", "../png")
        with self.assertRaises(StorageError):
            store.save_asset(b"")
        session = store.session("alice")
        session["last_image"] = referenced.name
        store.save_session("alice", session)
        store.append_history({"image": f"/assets/{referenced.name}"})
        store.append_history({"image": f"/assets/{obsolete.name}"})
        store.append_history({"image": f"/assets/{recent.name}"})
        removed = store.cleanup_history(1)
        self.assertEqual(2, removed["history_deleted"])
        self.assertEqual(1, removed["assets_deleted"])
        self.assertTrue(referenced.exists())
        self.assertFalse(obsolete.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(referenced, store.asset(referenced.name))
        self.assertEqual(referenced, store.save_asset(b"referenced-fixture"))

    def test_save_all_is_atomic_on_serialisation_failure(self):
        first = self.store()
        first.settings["current_persona"] = "new-value"
        first.runtime["invalid"] = float("nan")
        with self.assertRaises(StorageError):
            first.save_all()
        first.close()
        second = self.store()
        self.assertEqual("default", second.settings["current_persona"])
        self.assertNotIn("invalid", second.runtime)

    def test_concurrent_session_saves_allow_exactly_one_matching_revision(self):
        first, second = self.store(), self.store()
        first.session("alice")
        values = [first.session("alice"), second.session("alice")]
        def save(pair):
            store, value = pair
            try:
                store.save_session("alice", value)
                return True
            except StorageConflictError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, zip((first, second), values)))
        self.assertEqual([False, True], sorted(results))

    def test_cleanup_bounds_terminal_records_without_deleting_live_work(self):
        store = self.store()
        with patch("storage.time.time", return_value=1000):
            old_asset = store.save_asset(b"old-job-fixture")
            old_job = store.create_job({"source": "webui", "status": "succeeded", "asset": old_asset.name})
            sending = store.create_job({"status": "sending"})
            generating = store.create_job({"status": "generating"})
            store.reserve_delivery("old-terminal")
            store.update_delivery("old-terminal", {"status": "sent", "job_id": old_job["id"]})
            store.reserve_delivery("old-sending")
            store.update_delivery("old-sending", {"status": "sending", "job_id": sending["id"]})
        with patch("storage.time.time", return_value=1000 + 31 * 86400):
            latest = store.create_job({"status": "sent"})
            ready_to_send = store.create_job({"status": "succeeded", "source": "chat"})
            removed = store.cleanup_history(1)
            self.assertEqual(1, removed["jobs_deleted"])
            self.assertEqual(1, removed["deliveries_deleted"])
            self.assertEqual(1, removed["assets_deleted"])
            self.assertIsNone(store.job(old_job["id"]))
            self.assertIsNotNone(store.job(sending["id"]))
            self.assertIsNotNone(store.job(generating["id"]))
            self.assertIsNotNone(store.job(ready_to_send["id"]))
            self.assertIsNotNone(store.job(latest["id"]))
            self.assertIsNone(store.delivery("old-terminal"))
            self.assertEqual("sending", store.delivery("old-sending")["status"])
            self.assertFalse(old_asset.exists())

    def test_read_session_does_not_create_switch_or_write_current_state(self):
        store = self.store()
        self.assertIsNone(store.read_session("missing"))
        self.assertEqual([], store.list_sessions())
        persona = store.upsert_persona({"name": "second"})
        session = store.session("conversation", persona["id"])
        session["state"]["outfit"] = "current outfit"
        session["pending"] = {"request": "current conditions"}
        saved = store.save_session("conversation", session)
        snapshot = store.read_session("conversation")
        self.assertEqual(saved, snapshot)
        snapshot["state"]["outfit"] = "caller mutation"
        self.assertEqual(saved, store.read_session("conversation"))
        # A reader also preserves residual data without requiring that a persona
        # still exist; only explicit session switching may recreate that data.
        saved["persona_id"] = "deleted-persona"
        with store._db:
            store._write_session(saved)
        self.assertEqual(saved, store.read_session("conversation"))

    @staticmethod
    def pending_photo(store, key="conversation", request_id="conditions-1"):
        session = store.session(key, "default")
        session["pending"] = {"session_key": key, "persona_id": "default", "request_id": request_id, "request_kind": "photo", "expires_at": time.time() + 1800, "requirements": {"outfit": "white sailor uniform"}}
        return store.save_session(key, session)

    @staticmethod
    def photo_job(store, key="conversation", **patch):
        return store.create_job({"status": "queued", "session_key": key, "persona_id": "default", **patch})

    def test_photo_confirmation_claim_is_atomic_durable_and_kept_on_failure(self):
        first = self.store()
        before = self.pending_photo(first)
        second = self.store()
        jobs = [self.photo_job(first), self.photo_job(second)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(store.claim_photo_request, "conversation", "default", "conditions-1", job["id"]) for store, job in zip((first, second), jobs)]
            results = [future.result() for future in futures]
        self.assertEqual(1, sum(result["claimed"] for result in results))
        winner = next(result["job_id"] for result in results if result["claimed"])
        self.assertEqual({winner}, {result["job_id"] for result in results})
        self.assertEqual(before["revision"] + 1, first.read_session("conversation")["revision"])
        first.update_job(winner, {"status": "failed"})
        first.cleanup_history(0)
        self.assertEqual("failed", first.job(winner)["status"])
        restarted = self.store()
        candidate = self.photo_job(restarted)
        duplicate = restarted.claim_photo_request("conversation", "default", "conditions-1", candidate["id"])
        self.assertFalse(duplicate["claimed"])
        self.assertEqual(winner, duplicate["job_id"])
        self.assertEqual("white sailor uniform", duplicate["session"]["pending"]["requirements"]["outfit"])

    def test_photo_confirmation_rejects_scope_expiry_kind_and_unbound_jobs(self):
        store = self.store()
        original = self.pending_photo(store)
        candidate = self.photo_job(store)
        for patch in ({"request_id": "other"}, {"session_key": "elsewhere"}, {"persona_id": "other"}, {"request_kind": "state"}, {"expires_at": time.time() - 1}):
            with self.subTest(patch=patch):
                session = store.read_session("conversation")
                session["pending"] = {**original["pending"], **patch}
                saved = store.save_session("conversation", session)
                with self.assertRaises(StorageConflictError):
                    store.claim_photo_request("conversation", "default", "conditions-1", candidate["id"])
                self.assertEqual(saved, store.read_session("conversation"))
        self.pending_photo(store)
        for job in ("missing", self.photo_job(store, key="other")["id"]):
            with self.subTest(job=job), self.assertRaises(StorageConflictError):
                store.claim_photo_request("conversation", "default", "conditions-1", job)

    def test_explicit_retry_rebinds_failed_claim_but_cannot_reopen_withdrawal(self):
        store = self.store()
        self.pending_photo(store)
        old = self.photo_job(store)
        store.claim_photo_request("conversation", "default", "conditions-1", old["id"])
        store.update_job(old["id"], {"status": "failed"})
        session = store.read_session("conversation")
        session["pending"]["expires_at"] = time.time() - 1
        store.save_session("conversation", session)
        retry = self.photo_job(store)
        result = store.rebind_photo_request("conversation", "default", "conditions-1", old["id"], retry["id"])
        self.assertTrue(result["claimed"])
        self.assertEqual(retry["id"], result["session"]["pending"]["execution_job_id"])
        self.assertFalse(store.rebind_photo_request("conversation", "default", "conditions-1", old["id"], retry["id"])["claimed"])
        store.update_job(retry["id"], {"status": "cancelled", "cancel_requested": True})
        another = self.photo_job(store)
        with self.assertRaises(StorageConflictError):
            store.rebind_photo_request("conversation", "default", "conditions-1", retry["id"], another["id"])
        self.pending_photo(store, request_id="new-conditions")
        with self.assertRaises(StorageConflictError):
            store.rebind_photo_request("conversation", "default", "conditions-1", old["id"], another["id"])

    def test_asset_lease_protects_reference_while_history_owner_is_pruned(self):
        store = self.store()
        asset = store.save_asset(b"current-reference-from-old-photo")
        store.create_job({"status": "failed", "asset": asset.name})
        store.append_history({"image": "/assets/" + asset.name})
        token = store.lease_asset(asset.name)
        removed = store.cleanup_history(0)
        self.assertEqual(1, removed["jobs_deleted"])
        self.assertEqual(0, removed["assets_deleted"])
        self.assertTrue(asset.exists())
        store.release_asset(token)
        self.assertEqual(1, store.cleanup_assets(0)["assets_deleted"])
        self.assertFalse(asset.exists())

    def test_independent_asset_leases_survive_restart_and_expire(self):
        first = self.store()
        asset = first.save_asset(b"parallel-reference")
        now = time.time()
        with patch("storage.time.time", return_value=now):
            lease1 = first.lease_asset(asset.name, ttl=60)
            first.lease_asset(asset.name, ttl=60)
        first.release_asset(lease1)
        first.release_asset(lease1)
        second = self.store()
        with patch("storage.time.time", return_value=now + 30):
            self.assertEqual(0, second.cleanup_assets(0)["assets_deleted"])
        with patch("storage.time.time", return_value=now + 61):
            result = second.cleanup_assets(0)
        self.assertEqual(1, result["leases_expired"])
        self.assertEqual(1, result["assets_deleted"])

    def test_orphan_cleanup_keeps_drafts_all_record_references_and_upload_files(self):
        store = self.store()
        now = time.time()
        referenced = [store.save_asset(f"record-{index}".encode()) for index in range(6)]
        store.upsert_persona({"id": "default", "reference_asset": referenced[0].name})
        session = store.session("conversation")
        session["last_image"] = referenced[1].name
        store.save_session("conversation", session)
        store.create_job({"status": "queued", "reference_assets": [referenced[2].name]})
        store.append_history({"image": "/assets/" + referenced[3].name})
        store.reserve_delivery("pending-delivery")
        store.update_delivery("pending-delivery", {"asset": referenced[4].name})
        store.record_action({"kind": "photo", "asset": referenced[5].name})
        orphan = store.save_asset(b"old-unrecorded-chat-image")
        for path in [*referenced, orphan]:
            os.utime(path, (now - 7200, now - 7200))
        draft = store.save_asset(b"fresh-upload-draft")
        partial = store.assets / ".asset-upload"
        partial.write_bytes(b"incomplete")
        other = store.assets / "notes.txt"
        other.write_text("not an image", encoding="utf-8")
        result = store.cleanup_assets(3600)
        self.assertEqual(1, result["assets_deleted"])
        self.assertFalse(orphan.exists())
        self.assertTrue(all(path.exists() for path in [*referenced, draft, partial, other]))

    def test_orphan_cleanup_observes_reference_updates_from_another_connection(self):
        first = self.store()
        asset = first.save_asset(b"reference-written-elsewhere")
        second = self.store()
        second.upsert_persona({"id": "default", "reference_asset": asset.name})
        self.assertEqual(0, first.cleanup_assets(0)["assets_deleted"])
        self.assertTrue(asset.exists())

    def test_reuploading_existing_asset_restarts_its_draft_grace_period(self):
        store = self.store()
        asset = store.save_asset(b"reused-draft")
        old = time.time() - 7200
        os.utime(asset, (old, old))
        self.assertEqual(asset, store.save_asset(b"reused-draft"))
        self.assertEqual(0, store.cleanup_assets(3600)["assets_deleted"])

    def test_v4_upgrade_preserves_records_and_adds_persistent_asset_leases(self):
        first = self.store()
        asset = first.save_asset(b"v4-reference")
        session = first.session("legacy")
        session["last_image"] = asset.name
        first.save_session("legacy", session)
        first.close()
        connection = self.connection()
        with connection:
            connection.execute("DROP TABLE asset_leases")
            connection.execute("PRAGMA user_version=4")
        connection.close()
        upgraded = self.store()
        self.assertEqual(asset.name, upgraded.read_session("legacy")["last_image"])
        self.assertTrue(upgraded.lease_asset(asset.name))
        self.assertEqual(1, len(list((self.directory / "migration_backups").glob("sqlite-v4-*"))))


if __name__ == "__main__":
    unittest.main()
