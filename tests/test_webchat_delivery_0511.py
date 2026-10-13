"""Offline WebChat delivery simulations, not a live AstrBot/WebChat test.

The fake adapter models AstrBot 4.28.2's request-scoped event queue and its
separate proactive history path. A real temporary SQLite database verifies
which conversation receives the payload; a routing return value alone is not
treated as evidence that WebChat persisted or displayed it.
"""
import asyncio
import json
import sqlite3
import types
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import test_core as fixtures


INSTANCE = "custom-local-browser"
UMO = INSTANCE + ":FriendMessage:alice!conversation-1"


class SimulatedWebChatPlatform:
    """Small behavior model of public sends, with explicitly injected failures."""

    def __init__(self, database):
        self.database = Path(database)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE webchat_history (session_id TEXT, payload TEXT)")
        self.request_queues = {}
        self.mode = "history"
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def meta(self):
        return types.SimpleNamespace(name="webchat", id=INSTANCE)

    def persist(self, session_id, chain):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO webchat_history VALUES (?, ?)", (session_id, json.dumps(chain.chain)))

    def history(self, session_id):
        # A fresh SQLite connection tests persistence beyond the send call.
        with closing(sqlite3.connect(self.database)) as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM webchat_history WHERE session_id=? ORDER BY rowid", (session_id,),
            )]

    async def send_by_session(self, session_id, chain):
        if self.mode == "paused":
            self.entered.set()
            await self.release.wait()
        if self.mode == "closing_queue":
            # The adapter selected the active route immediately before the
            # request closes; its queue is no longer drained or persisted.
            queue = self.request_queues.setdefault(session_id, [])
            self.request_queues.pop(session_id)
            queue.append(chain)
            return None
        if self.mode == "swallowed_history_error":
            try:
                raise sqlite3.OperationalError("simulated history persistence failure")
            except sqlite3.OperationalError:
                return None
        self.persist(session_id, chain)
        return None


class SimulatedWebChatContext(fixtures.Context):
    def __init__(self, platform):
        super().__init__()
        self.platform = platform
        self.route_result = True

    def get_platform_inst(self, platform_id):
        return self.platform if platform_id == INSTANCE else None

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        if self.route_result is False:
            return False
        await self.platform.send_by_session(umo.split(":", 2)[2], chain)
        return self.route_result


class SimulatedWebChatEvent(fixtures.Event):
    def __init__(self, platform, *, streaming=False, message_id="webchat-photo"):
        super().__init__("看看自拍", UMO)
        self.platform = platform
        self.streaming = streaming
        self.request_open = True
        self.event_send_calls = 0
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        platform.request_queues[self.session_id] = []

    @property
    def session_id(self):
        return self.unified_msg_origin.split(":", 2)[2]

    def get_platform_id(self):
        return INSTANCE

    def get_platform_name(self):
        return "webchat"

    def get_platform(self):
        return self.platform.meta()

    def close_request(self):
        self.request_open = False
        self.platform.request_queues.pop(self.session_id, None)

    async def send(self, chain):
        self.event_send_calls += 1
        # A None result is normal for event.send and is also the silent-drop
        # behavior when its original request is no longer being consumed.
        queue = self.platform.request_queues.get(self.session_id)
        if not self.request_open or queue is None:
            return None
        queue.append(chain)
        self.sent.append(chain)
        return None


class WebChatDelivery0511Tests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.RuntimeTests.asyncSetUp(self)
        self.platform = SimulatedWebChatPlatform(Path(self.temp.name) / "platform-history.sqlite3")
        self.context = SimulatedWebChatContext(self.platform)
        self.plugin.context = self.context
        self.plugin.dialogue.context = self.context

    async def complete_native_after_request(self, *, streaming=False):
        event = SimulatedWebChatEvent(self.platform, streaming=streaming)
        self.image_provider.pause = asyncio.Event()
        result = json.loads(await self.plugin.tool_photo(event, "portrait, soft daylight", caption="给你看看。"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "queued")
        event.close_request()
        self.image_provider.pause.set()
        await asyncio.gather(*list(self.plugin._tasks))
        return event, self.store.job(result["job_id"])

    async def saved_photo(self):
        env = await self.plugin.dialogue.environment(UMO)
        return self.store.create_job({
            "status": "succeeded", "phase": "generated", "umo": UMO,
            "session_key": env["key"], "persona_id": env["persona"]["id"],
            "asset": self.store.save_asset(fixtures.image_bytes(), "png").name,
            "caption": "照片。", "source": "chat", "mode": "persona",
        })

    def assert_unverified_photo(self, job):
        self.assertEqual(job["status"], "uncertain", job)
        self.assertEqual(job.get("delivery_evidence"), "unverified", job)
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        if job.get("delivery_key"):
            self.assertEqual(self.store.delivery(job["delivery_key"])["status"], "uncertain")

    def assert_original_history_image(self, job):
        rows = self.platform.history(UMO.split(":", 2)[2])
        images = [item[1] for row in rows for item in row if item[0] == "image"]
        self.assertEqual(images, [str(self.store.asset(job["asset"]))])
        self.assertFalse(self.platform.history("alice!other-conversation"))

    async def test_simulated_expired_event_none_reproduces_silent_loss(self):
        event = SimulatedWebChatEvent(self.platform)
        event.close_request()
        self.assertIsNone(await event.send(fixtures.Chain().message("late photo")))
        self.assertFalse(self.platform.history(event.session_id))

    async def test_native_non_streaming_after_request_uses_durable_original_history(self):
        event, job = await self.complete_native_after_request()
        self.assertEqual(event.event_send_calls, 0)
        self.assertEqual([umo for umo, _ in self.context.sent], [UMO])
        self.assert_original_history_image(job)
        self.assert_unverified_photo(job)
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_native_streaming_after_request_uses_durable_original_history(self):
        event, job = await self.complete_native_after_request(streaming=True)
        self.assertTrue(event.streaming)
        self.assertEqual(event.event_send_calls, 0)
        self.assert_original_history_image(job)
        self.assert_unverified_photo(job)

    async def test_native_background_preserves_event_platform_without_lookup(self):
        self.context.get_platform_inst = None
        event, job = await self.complete_native_after_request()
        self.assertEqual(event.get_platform_name(), "webchat")
        self.assertNotIn("webchat", event.unified_msg_origin.lower())
        self.assertEqual(job.get("delivery_platform"), "webchat")
        self.assertEqual(event.event_send_calls, 0)
        self.assert_original_history_image(job)
        self.assert_unverified_photo(job)

    async def test_queued_chat_job_uses_context_after_request_is_closed(self):
        event = SimulatedWebChatEvent(self.platform)
        env = await self.plugin._env(event)
        self.image_provider.pause = asyncio.Event()
        result = await self.plugin._queue_job(env, {
            "decision": "photo", "prompt": "portrait", "state_patch": {}, "reply": "再拍一张。",
        }, event.message_str, {}, event=event)
        event.close_request()
        self.image_provider.pause.set()
        await asyncio.gather(*list(self.plugin._tasks))
        job = self.store.job(result["job_id"])
        self.assertEqual(event.event_send_calls, 0)
        self.assert_original_history_image(job)
        self.assert_unverified_photo(job)

    async def test_failed_native_retry_finishes_in_original_history_after_retry_request(self):
        self.image_provider.error = "first drawing failed"
        _, original = await self.complete_native_after_request()
        self.assertEqual(original["status"], "failed")
        self.image_provider.error = None
        self.image_provider.pause = asyncio.Event()
        retry_event = SimulatedWebChatEvent(self.platform, message_id="retry-webchat-photo")
        retry_event.message_str = "重试刚才那张自拍"
        result = await self.plugin._retry_job({"id": original["id"]}, event=retry_event)
        retry_event.close_request()
        self.image_provider.pause.set()
        await asyncio.gather(*list(self.plugin._tasks))
        job = self.store.job(result["job_id"])
        self.assertEqual(job["retry_of"], original["id"])
        self.assertEqual(retry_event.event_send_calls, 0)
        self.assert_original_history_image(job)
        self.assert_unverified_photo(job)
        self.assertEqual(len(self.image_provider.calls), 2)

    async def test_context_none_return_is_unverified_even_when_history_contains_image(self):
        self.context.route_result = None
        job = await self.saved_photo()
        await self.plugin._send_job(job, umo=UMO)
        self.assert_original_history_image(job)
        self.assert_unverified_photo(self.store.job(job["id"]))

    async def test_arbitrary_instance_id_is_identified_from_platform_metadata(self):
        self.assertNotIn("webchat", UMO.lower())
        job = await self.saved_photo()
        await self.plugin._send_job(job, umo=UMO)
        self.assert_original_history_image(job)
        self.assert_unverified_photo(self.store.job(job["id"]))

    async def test_event_metadata_identifies_webchat_when_platform_lookup_is_unavailable(self):
        event = SimulatedWebChatEvent(self.platform)
        event.close_request()
        self.context.get_platform_inst = None
        job = await self.saved_photo()
        await self.plugin._send_job(job, event=event, umo=UMO)
        self.assertEqual(event.event_send_calls, 0)
        self.assert_original_history_image(job)
        self.assert_unverified_photo(self.store.job(job["id"]))

    async def test_active_queue_closing_race_is_never_reported_as_sent(self):
        self.platform.mode = "closing_queue"
        event, job = await self.complete_native_after_request(streaming=True)
        self.assertFalse(self.platform.history(event.session_id))
        self.assertEqual(event.event_send_calls, 0)
        self.assert_unverified_photo(job)
        self.assertEqual(len(self.context.sent), 1)

    async def test_swallowed_platform_history_failure_is_never_reported_as_sent(self):
        self.platform.mode = "swallowed_history_error"
        event, job = await self.complete_native_after_request()
        self.assertFalse(self.platform.history(event.session_id))
        self.assertEqual(event.event_send_calls, 0)
        self.assert_unverified_photo(job)
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_explicit_context_false_keeps_image_and_marks_delivery_failed(self):
        self.context.route_result = False
        job = await self.saved_photo()
        with self.assertRaises(fixtures.main.DeliveryRejectedError):
            await self.plugin._send_job(job, umo=UMO)
        saved = self.store.job(job["id"])
        self.assertEqual(saved["status"], "delivery_failed")
        self.assertTrue(self.store.asset(saved["asset"]).is_file())
        self.assertFalse(self.platform.history(UMO.split(":", 2)[2]))
        self.assertEqual(len(self.context.sent), 1)
        self.assertFalse(self.image_provider.calls)

    async def test_webchat_failure_notification_true_is_unverified_not_accepted(self):
        job = self.store.create_job({"status": "failed", "umo": UMO, "error": "drawing failed"})
        event = SimulatedWebChatEvent(self.platform)
        event.close_request()
        await self.plugin._notify_photo_failure(job, event=event)
        saved = self.store.job(job["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["notification_state"], "uncertain")
        self.assertEqual(saved["notification_channel"], "context")
        rows = self.platform.history(event.session_id)
        self.assertEqual(len(rows), 1)
        self.assertIn("drawing failed", json.dumps(rows))
        self.assertEqual(event.event_send_calls, 0)
        await self.plugin._notify_photo_failure(job, event=event)
        self.assertEqual(len(self.context.sent), 1)

    async def test_native_generation_failure_notification_remains_unverified(self):
        self.image_provider.error = "simulated provider error"
        event, job = await self.complete_native_after_request(streaming=True)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["notification_state"], "uncertain")
        self.assertEqual(job["notification_channel"], "context")
        self.assertEqual(event.event_send_calls, 0)
        rows = self.platform.history(event.session_id)
        self.assertIn("simulated provider error", json.dumps(rows))
        self.assertFalse([item for row in rows for item in row if item[0] == "image"])
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_webchat_without_context_send_never_falls_back_to_expired_event(self):
        event = SimulatedWebChatEvent(self.platform)
        event.close_request()
        self.context.send_message = None
        job = await self.saved_photo()
        try:
            await self.plugin._send_job(job, event=event, umo=UMO)
        except Exception:
            pass
        self.assertEqual(event.event_send_calls, 0)
        self.assertNotEqual(self.store.job(job["id"])["status"], "sent")
        self.assertTrue(self.store.asset(job["asset"]).is_file())

    async def test_webchat_notification_without_context_never_falls_back_to_event(self):
        event = SimulatedWebChatEvent(self.platform)
        event.close_request()
        self.context.send_message = None
        job = self.store.create_job({"status": "failed", "umo": UMO, "error": "drawing failed"})
        await self.plugin._notify_photo_failure(job, event=event)
        self.assertEqual(event.event_send_calls, 0)
        self.assertEqual(self.store.job(job["id"])["notification_state"], "failed")

    async def test_manual_resend_preserves_original_asset_without_regeneration(self):
        original = await self.saved_photo()
        self.store.update_job(original["id"], {"status": "uncertain"})
        result = await self.plugin.web_resend_job({"id": original["id"], "confirm_uncertain": True})
        job = self.store.job(original["id"])
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(job["asset"], original["asset"])
        self.assertEqual(job["resend_count"], 1)
        self.assert_original_history_image(job)
        self.assert_unverified_photo(job)
        self.assertFalse(self.image_provider.calls)
        with self.assertRaises(ValueError):
            await self.plugin.web_resend_job({"id": original["id"]})
        self.assertEqual(len(self.context.sent), 1)

    async def test_legacy_sent_requires_explicit_confirmation_to_resend_original(self):
        original = await self.saved_photo()
        self.store.update_job(original["id"], {"status": "sent", "phase": "sent"})
        unchanged = await self.plugin.web_resend_job({"id": original["id"]})
        self.assertEqual(unchanged["status"], "sent")
        self.assertTrue(unchanged["reused"])
        self.assertFalse(self.context.sent)
        self.assertFalse(self.platform.history(UMO.split(":", 2)[2]))
        result = await self.plugin.web_resend_job({"id": original["id"], "confirm_uncertain": True})
        saved = self.store.job(original["id"])
        self.assertEqual(result["status"], "uncertain")
        self.assertIs(result["ok"], False)
        self.assertIs(result["generated"], False)
        self.assertEqual(saved["asset"], original["asset"])
        self.assertEqual(saved["resend_count"], 1)
        self.assertEqual([umo for umo, _ in self.context.sent], [UMO])
        self.assert_original_history_image(saved)
        self.assert_unverified_photo(saved)
        self.assertFalse(self.image_provider.calls)

    async def test_concurrent_legacy_sent_recovery_sends_original_only_once(self):
        original = await self.saved_photo()
        self.store.update_job(original["id"], {"status": "sent", "phase": "sent"})
        lock = self.plugin._lock("delivery:" + original["id"])
        await lock.acquire()
        # Both requests observe the same resend_count before either acquires
        # the shared delivery lock, matching simultaneous console submissions.
        body = {"id": original["id"], "confirm_uncertain": True}
        first = asyncio.create_task(self.plugin.web_resend_job(body))
        second = asyncio.create_task(self.plugin.web_resend_job(body))
        try:
            await asyncio.sleep(0)
        finally:
            lock.release()
        results = await asyncio.gather(first, second)
        saved = self.store.job(original["id"])
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(saved["resend_count"], 1)
        self.assertEqual([result["status"] for result in results], ["uncertain", "uncertain"])
        self.assertEqual(sum(result.get("reused") is True for result in results), 1)
        self.assert_original_history_image(saved)
        self.assert_unverified_photo(saved)
        self.assertFalse(self.image_provider.calls)

    def setup_webchat_morning(self, *, with_image):
        scheduler = fixtures.ActiveScheduler(self.plugin)
        scheduler._in_window = lambda *_args: True
        self.store.settings["active"].update(enabled=False, min_idle_sec=0, min_gap_sec=60)
        self.store.settings["good_morning"].update(enabled=True, max_attempts=3, retry_interval_sec=900)
        self.store.settings["proactive_budget"].update(messages=10, llm=20, photos=10)
        event = SimulatedWebChatEvent(self.platform)
        scheduler.observe(event)
        event.close_request()
        target = self.store.targets["items"][0]
        target.update(enabled=True, last_inbound=0, last_active=0, with_image=with_image)
        self.store.save_targets()
        date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        return scheduler, target, f"morning:{UMO}:{date}"

    async def assert_morning_not_automatically_retried(self, scheduler, target, key):
        self.assertEqual(self.store.delivery(key)["status"], "uncertain")
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(len(self.context.llm.calls), 1)
        # Even with an old retry timestamp, an ambiguous delivery remains
        # terminal and must not consume another model or platform send call.
        self.store.update_delivery(key, {"retry_at": 1})
        target.update(last_active=0, morning_date="", next_opportunity=0)
        self.store.save_targets()
        await scheduler.tick(wait=True)
        self.assertEqual(self.store.delivery(key)["status"], "uncertain")
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(len(self.context.llm.calls), 1)

    async def test_morning_photo_keeps_unverified_delivery_without_automatic_retry(self):
        scheduler, target, key = self.setup_webchat_morning(with_image=True)
        self.context.llm.next = {"decision": "photo", "reply": "早安。", "prompt": "morning portrait", "state_patch": {}}
        await scheduler.tick(wait=True)
        delivery = self.store.delivery(key)
        job = self.store.job(delivery["job_id"])
        self.assert_unverified_photo(job)
        self.assert_original_history_image(job)
        await self.assert_morning_not_automatically_retried(scheduler, target, key)
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_morning_text_unknown_receipt_is_not_automatically_retried(self):
        scheduler, target, key = self.setup_webchat_morning(with_image=False)
        self.context.llm.next = {"decision": "chat", "reply": "早安，今天也一起好好生活吧。"}
        await scheduler.tick(wait=True)
        rows = self.platform.history(UMO.split(":", 2)[2])
        self.assertEqual(len(rows), 1)
        self.assertIn("早安", json.dumps(rows, ensure_ascii=False))
        await self.assert_morning_not_automatically_retried(scheduler, target, key)
        self.assertFalse(self.image_provider.calls)

    async def test_cancel_during_context_send_retains_uncertainty_without_retry(self):
        self.platform.mode = "paused"
        event = SimulatedWebChatEvent(self.platform)
        event.close_request()
        result = json.loads(await self.plugin.tool_photo(event, "portrait"))
        self.assertTrue(result["ok"], result)
        await asyncio.wait_for(self.platform.entered.wait(), 1)
        task = self.plugin._job_tasks[result["job_id"]]
        self.assertEqual(self.store.job(result["job_id"])["status"], "sending")
        with self.assertRaises(ValueError):
            await self.plugin.web_cancel_job({"id": result["job_id"]})
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        job = self.store.job(result["job_id"])
        self.assertEqual(job["status"], "uncertain")
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(event.event_send_calls, 0)
        self.assertFalse(job.get("notification_state"))
        self.assertFalse(self.platform.history(event.session_id))
        self.assertEqual(len(self.image_provider.calls), 1)
        with self.assertRaises(ValueError):
            await self.plugin.web_retry_job({"id": job["id"]})

    async def test_non_webchat_event_none_remains_compatible(self):
        event = fixtures.Event("看看自拍")
        job = await self.saved_photo()
        job = self.store.update_job(job["id"], {"umo": event.unified_msg_origin})
        await self.plugin._send_job(job, event=event)
        self.assertEqual(self.store.job(job["id"])["status"], "sent")
        self.assertEqual(len(event.sent), 1)
        self.assertFalse(self.context.sent)


if __name__ == "__main__":
    unittest.main()
