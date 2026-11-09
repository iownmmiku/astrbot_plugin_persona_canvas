"""0.5.17: event-loop offloading, deferred target writes and shared helpers."""
import asyncio
import gc
import json
import os
import time
import types
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfoNotFoundError

import test_core as fixtures
import test_comfyui as comfy
from test_core import Event, TextPart, image_bytes
from canvas_test import common, page_api
from canvas_test.active import ActiveScheduler
from canvas_test.providers import base


class SharedHelperTests(unittest.TestCase):
    def test_default_zones_work_without_tzdata_and_unknown_zones_fail_clearly(self):
        common.zone.cache_clear()
        try:
            with patch.object(common, "ZoneInfo", side_effect=ZoneInfoNotFoundError("missing tzdata")):
                shanghai = common.zone("Asia/Shanghai")
                self.assertEqual(shanghai.utcoffset(None).total_seconds(), 8 * 3600)
                self.assertEqual(common.zone("UTC").utcoffset(None).total_seconds(), 0)
                with self.assertRaises(ZoneInfoNotFoundError) as error:
                    common.zone("Europe/Berlin")
            self.assertIsInstance(error.exception, common.UnknownTimezoneError)
            self.assertIn("Europe/Berlin", str(error.exception))
            self.assertNotIn("'", str(error.exception))
        finally:
            common.zone.cache_clear()

    def test_prompt_redaction_hides_keys_but_keeps_ordinary_wording(self):
        storage = types.SimpleNamespace(settings={"providers": {"x": {"api_key": "k-12345678"}}}, _scrub_known_secrets=lambda value: value)
        text = "a bearer of gifts, key k-12345678"
        self.assertEqual(common.redact_text(storage, text, limit=None, patterns=False), "a bearer of gifts, key [已隐藏]")
        self.assertIn("bearer [已隐藏]", common.redact_text(storage, "bearer abc.def"))

    def test_image_verification_is_cached_by_content(self):
        data = image_bytes()
        with base._IMAGE_INFO_LOCK:
            base._IMAGE_INFO_CACHE.clear()
        with patch.object(base, "_decode_image_info", wraps=base._decode_image_info) as decode:
            first = base._image_info(data)
            self.assertEqual(base._image_info(bytes(data)), first)
        self.assertEqual(decode.call_count, 1)
        with self.assertRaises(base.ProviderError), patch.object(base, "_decode_image_info", wraps=base._decode_image_info) as decode:
            base._image_info(b"not an image")
        with self.assertRaises(base.ProviderError):
            base._image_info(b"not an image")


class ProviderTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_comfy_polling_reuses_one_session_and_backs_off(self):
        sessions, captures = [], []
        pending = {"left": 3}

        class Reply:
            status = 200
            def __init__(self, raw, mime):
                self.raw, self.headers, self.content = raw, {"Content-Type": mime}, self
            async def iter_chunked(self, size):
                yield self.raw
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class Session:
            closed = False
            def __init__(self, **kwargs):
                sessions.append(kwargs)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def request(self, method, url, **kwargs):
                captures.append((method, url, kwargs))
                if url.endswith("/prompt"):
                    raw, mime, _ = comfy.json_reply({"prompt_id": "task-123"})
                elif "/history/" in url:
                    value = {} if pending["left"] else comfy.history()
                    pending["left"] = max(0, pending["left"] - 1)
                    raw, mime, _ = comfy.json_reply(value)
                else:
                    raw, mime = comfy.PNG, "image/png"
                return Reply(raw, mime)

        item = comfy.provider()
        item.poll_interval, item.max_poll_interval = 0.5, 1.0
        delays = []
        real_sleep = asyncio.sleep
        async def sleep(delay, *args, **kwargs):
            delays.append(delay)
            await real_sleep(0)
        with patch("aiohttp.ClientSession", Session), patch("providers.comfyui.asyncio.sleep", sleep):
            result = await item.generate("portrait", "")
        self.assertEqual(result.data, comfy.PNG)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len([c for c in captures if "/history/" in c[1]]), 4)
        self.assertTrue(all("timeout" in kwargs for _method, _url, kwargs in captures))
        self.assertEqual(delays[:3], [0.5, 0.75, 1.0])

    async def test_http_error_keeps_message_generic_and_detail_redacted(self):
        class Reply:
            status = 400
            headers = {"Content-Type": "application/json"}
            def __init__(self):
                self.content = self
            async def iter_chunked(self, size):
                yield json.dumps({"error": {"message": "model foo not found; key private-workflow-secret"}}).encode()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class Session:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def request(self, *args, **kwargs):
                return Reply()

        workflow = comfy.copy.deepcopy(comfy.WORKFLOW)
        workflow["10"]["inputs"]["api_key"] = "private-workflow-secret"
        item = comfy.provider(workflow=workflow)
        with patch("aiohttp.ClientSession", Session), self.assertRaises(comfy.ProviderError) as error:
            await item.generate("portrait", "")
        self.assertNotIn("model foo", str(error.exception))
        self.assertIn("model foo not found", error.exception.detail)
        self.assertNotIn("private-workflow-secret", error.exception.detail)


class Runtime0517Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    async def test_repeated_messages_defer_inbound_writes_until_flush(self):
        scheduler = ActiveScheduler(self.plugin)
        with patch.object(self.store, "save_targets", wraps=self.store.save_targets) as save:
            scheduler.observe(Event())
            self.assertEqual(save.call_count, 1)  # A new target is saved at once.
            for _ in range(5):
                scheduler.observe(Event())
            self.assertEqual(save.call_count, 1)
            self.assertIsNotNone(scheduler._flush_handle)
            target = self.store.targets["items"][0]
            target["unanswered"] = 2
            scheduler.observe(Event())  # Resetting a counter is a real change.
            self.assertEqual(save.call_count, 2)
            scheduler.observe(Event())
            scheduler.flush()
            self.assertEqual(save.call_count, 3)
            self.assertIsNone(scheduler._flush_handle)
        stored = self.store._read_document("targets")["items"][0]
        self.assertEqual(stored["last_inbound"], target["last_inbound"])

    async def test_plugin_stop_persists_deferred_inbound_time(self):
        self.plugin.active = ActiveScheduler(self.plugin)
        self.plugin.active.observe(Event())
        before = self.store.targets["items"][0]["last_inbound"]
        time.sleep(0.01)
        self.plugin.active.observe(Event())
        latest = self.store.targets["items"][0]["last_inbound"]
        self.assertGreater(latest, before)
        self.assertEqual(self.store._read_document("targets")["items"][0]["last_inbound"], before)
        with patch.object(self.store, "close"):
            await self.plugin.terminate()
        self.assertEqual(self.store._read_document("targets")["items"][0]["last_inbound"], latest)
        self.plugin._closing = False

    async def test_session_locks_are_shared_while_used_and_then_released(self):
        lock = self.plugin._lock("delivery:job-1")
        self.assertIs(self.plugin._lock("delivery:job-1"), lock)
        async with lock:
            self.assertIn("delivery:job-1", self.plugin._session_locks)
        del lock
        gc.collect()
        self.assertNotIn("delivery:job-1", self.plugin._session_locks)

    async def test_settings_reject_wrong_types_and_fractional_integers(self):
        for patch_body in ({"llm": {"model": {"name": "x"}}}, {"generation": {"sampler": 3}}, {"generation": {"steps": 20.5}}, {"generation": {"width": None}}, {"active": {"timezone": ""}}, {"active": {"default_start": 900}}):
            with self.subTest(body=patch_body), self.assertRaises(ValueError):
                await self.plugin.web_save_settings(patch_body)
        saved = await self.plugin.web_save_settings({"generation": {"steps": "30", "sampler": "  euler  "}, "llm": {"model": "m"}, "unknown": {"x": 1}})
        self.assertEqual(saved["generation"]["steps"], 30)
        self.assertEqual(saved["generation"]["sampler"], "euler")
        self.assertEqual(self.store.settings["llm"]["model"], "m")

    async def test_asset_scan_keeps_unix_and_windows_paths_and_deletes_orphans(self):
        kept_unix = self.store.save_asset(image_bytes(), "png").name
        kept_windows = self.store.save_asset(b"windows-path-fixture", "jpg").name
        orphan = self.store.save_asset(b"orphan-fixture", "jpg").name
        self.store.append_history({"image": "/assets/" + kept_unix})
        job = self.store.create_job({"status": "failed", "note": "C:\\data\\assets\\" + kept_windows})
        self.assertIn("\\", self.store._db.execute("SELECT payload FROM jobs WHERE id=?", (job["id"],)).fetchone()[0])
        old = time.time() - 7200
        for name in (kept_unix, kept_windows, orphan):
            os.utime(self.store.asset(name), (old, old))
        result = self.store.cleanup_assets(grace_sec=3600)
        self.assertEqual(result["assets_deleted"], 1)
        self.assertTrue(self.store.asset(kept_unix).is_file())
        self.assertTrue(self.store.asset(kept_windows).is_file())
        with self.assertRaises(FileNotFoundError):
            self.store.asset(orphan)

    async def test_restart_recovery_reads_only_interrupted_or_attached_deliveries(self):
        for key, patch_body in (("done", {"status": "skipped", "kind": "reply_image"}), ("busy", {"status": "generating", "kind": "chat"}), ("attached", {"status": "decided", "kind": "reply_image", "attached": True}), ("old-attached", {"status": "uncertain", "kind": "reply_image", "attached": True})):
            self.store.reserve_delivery(key)
            self.store.update_delivery(key, patch_body)
        self.assertEqual({item["key"] for item in self.store.deliveries_for_recovery()}, {"busy", "attached", "old-attached"})
        self.store.create_job({"status": "sending"})
        self.store.create_job({"status": "sent"})
        self.assertEqual([job["status"] for job in self.store.jobs_with_status({"sending", "queued"})], ["sending"])

    async def test_llm_hook_falls_back_to_system_prompt_without_temporary_parts(self):
        module = types.ModuleType("astrbot.core.agent.message")
        req = types.SimpleNamespace(system_prompt="原系统提示", prompt="用户原话", contexts=[], conversation=None, extra_user_content_parts=[])
        with patch.dict("sys.modules", {"astrbot.core.agent.message": module}):
            await self.plugin.inject_visual_state(Event("随便聊聊"), req)
            first = req.system_prompt
            await self.plugin.inject_visual_state(Event("随便聊聊"), req)
        self.assertTrue(first.startswith("原系统提示\n\n"))
        self.assertEqual(req.system_prompt, first)
        self.assertEqual(req.extra_user_content_parts, [])
        # With the SDK available again, the copy in the system prompt is removed.
        await self.plugin.inject_visual_state(Event("随便聊聊"), req)
        self.assertEqual(req.system_prompt, "原系统提示")
        self.assertIsInstance(req.extra_user_content_parts[-1], TextPart)

    async def test_async_thumbnail_matches_sync_and_uses_shared_cache(self):
        name = self.store.save_asset(image_bytes(), "png").name
        value = await page_api.image_data_url_async(self.plugin, name, thumbnail=True)
        self.assertTrue(value.startswith("data:image/jpeg;base64,"))
        with patch.object(page_api, "_render_thumbnail", side_effect=AssertionError("cache miss")):
            self.assertEqual(page_api.image_data_url(self.plugin, name, thumbnail=True), value)
            self.assertEqual(await page_api.image_data_url_async(self.plugin, name, thumbnail=True), value)
        self.assertEqual(await page_api.image_data_url_async(self.plugin, "missing.png", thumbnail=True), "")


if __name__ == "__main__":
    unittest.main()
