"""Background photo failures with real jobs and mocked AstrBot delivery IO."""
import asyncio
import json
import types
import unittest
from unittest.mock import patch

import test_core as fixtures
from test_core import Event


class NativeFailureNotificationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    async def native(self, event=None):
        event = event or Event("看看自拍")
        result = json.loads(await self.plugin.tool_photo(event, "portrait, soft daylight"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "queued")
        await asyncio.gather(*list(self.plugin._tasks))
        return event, self.store.job(result["job_id"])

    async def test_native_provider_initialization_failure_uses_durable_context_channel(self):
        selections = 0
        def unavailable(_name=None):
            nonlocal selections
            selections += 1
            if selections == 1:
                return self.image_provider
            raise ValueError("provider initialization failed")
        self.plugin._provider = unavailable
        event = Event("看看自拍")
        async def expired(_chain):
            self.fail("the completed event must not be used when context delivery is available")
        event.send = expired
        _, job = await self.native(event)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["failure_stage"], "provider")
        self.assertIs(job["request_summary"]["submitted"], False)
        self.assertEqual(job["notification_state"], "accepted")
        self.assertEqual(job["notification_channel"], "context")
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.context.sent[0][0], event.unified_msg_origin)
        self.assertIn("provider initialization failed", str(self.context.sent[0][1].chain))
        self.assertFalse(self.image_provider.calls)

    async def test_platform_rejecting_failure_notice_is_recorded_without_event_retry(self):
        self.image_provider.error = "drawing failed"
        async def rejected(umo, chain):
            self.context.sent.append((umo, chain))
            return False
        self.context.send_message = rejected
        with self.assertLogs("canvas-tests", level="WARNING"):
            event, job = await self.native()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["failure_stage"], "provider")
        self.assertEqual(job["notification_state"], "failed")
        self.assertEqual(job["notification_channel"], "context")
        self.assertTrue(job["notification_error"])
        self.assertEqual(len(self.context.sent), 1)
        self.assertFalse(event.sent)

    async def test_notice_transport_exception_is_uncertain_and_redacted_without_fallback(self):
        self.image_provider.error = "original drawing failure"
        secret = "private-provider-token"
        self.store.settings["providers"]["default"]["api_key"] = secret
        async def broken(umo, chain):
            self.context.sent.append((umo, chain))
            raise ConnectionError("adapter disconnected " + secret + " Bearer exposed-token")
        self.context.send_message = broken
        with self.assertLogs("canvas-tests", level="WARNING") as logs:
            event, job = await self.native()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "original drawing failure")
        self.assertEqual(job["notification_state"], "uncertain")
        self.assertEqual(job["notification_channel"], "context")
        self.assertIn("adapter disconnected", job["notification_error"])
        self.assertNotIn(secret, job["notification_error"])
        self.assertNotIn("exposed-token", job["notification_error"])
        self.assertNotIn(secret, " ".join(logs.output))
        self.assertFalse(event.sent)
        self.assertEqual(len(self.context.sent), 1)

    async def test_event_channel_is_used_only_when_context_channel_is_unavailable(self):
        self.image_provider.error = "drawing failed"
        self.context.send_message = None
        event, job = await self.native()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["notification_state"], "accepted")
        self.assertEqual(job["notification_channel"], "event")
        self.assertEqual(len(event.sent), 1)
        self.assertIn("drawing failed", str(event.sent[0].chain))
        self.assertFalse(self.context.sent)

    async def test_no_delivery_channel_is_recorded_as_failure(self):
        self.image_provider.error = "drawing failed"
        self.context.send_message = None
        event = Event("看看自拍")
        event.send = None
        with self.assertLogs("canvas-tests", level="WARNING"):
            _, job = await self.native(event)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["notification_state"], "failed")
        self.assertEqual(job["notification_channel"], "none")
        self.assertTrue(job["notification_error"])
        self.assertFalse(self.context.sent)

    async def test_event_rejection_is_recorded_and_does_not_change_generation_failure(self):
        self.image_provider.error = "drawing failed"
        self.context.send_message = None
        event = Event("看看自拍")
        async def rejected(chain):
            event.sent.append(chain)
            return False
        event.send = rejected
        with self.assertLogs("canvas-tests", level="WARNING"):
            _, job = await self.native(event)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "drawing failed")
        self.assertEqual(job["notification_state"], "failed")
        self.assertEqual(job["notification_channel"], "event")
        self.assertEqual(len(event.sent), 1)

    async def test_empty_destination_uses_event_instead_of_context(self):
        event = Event("看看自拍")
        job = self.store.create_job({"status": "failed", "umo": "", "mode": "persona", "error": "drawing failed"})
        await self.plugin._notify_photo_failure(job, event=event)
        saved = self.store.job(job["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["notification_state"], "accepted")
        self.assertEqual(saved["notification_channel"], "event")
        self.assertEqual(len(event.sent), 1)
        self.assertFalse(self.context.sent)

    async def test_event_transport_exception_is_recorded_as_uncertain(self):
        self.image_provider.error = "drawing failed"
        self.context.send_message = None
        event = Event("看看自拍")
        async def broken(_chain):
            raise ConnectionError("event delivery interrupted")
        event.send = broken
        with self.assertLogs("canvas-tests", level="WARNING"):
            _, job = await self.native(event)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["notification_state"], "uncertain")
        self.assertEqual(job["notification_channel"], "event")
        self.assertIn("event delivery interrupted", job["notification_error"])
        self.assertFalse(self.context.sent)

    async def test_concurrent_failure_handlers_do_not_send_duplicate_notices(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def paused(umo, chain):
            self.context.sent.append((umo, chain))
            entered.set()
            await release.wait()
            return True
        self.context.send_message = paused
        event = Event("看看自拍")
        job = self.store.create_job({"status": "failed", "umo": event.unified_msg_origin, "mode": "persona", "error": "drawing failed"})
        first = asyncio.create_task(self.plugin._notify_photo_failure(job, event=event))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.plugin._notify_photo_failure(job, event=event)
        finally:
            release.set()
            await first
        # Re-reading the stale failed object after completion also remains safe.
        await self.plugin._notify_photo_failure(job, event=event)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.store.job(job["id"])["notification_state"], "accepted")
        self.assertFalse(event.sent)

    async def test_queued_chat_retry_path_also_reports_failure_via_context(self):
        self.image_provider.error = "retry drawing failed"
        event = Event("重试刚才那张自拍")
        env = await self.plugin._env(event)
        queued = await self.plugin._queue_job(env, {"decision": "photo", "prompt": "portrait", "state_patch": {}, "reply": "再试一次。"}, event.message_str, {}, event=event)
        await asyncio.gather(*list(self.plugin._tasks))
        job = self.store.job(queued["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["notification_state"], "accepted")
        self.assertEqual(job["notification_channel"], "context")
        self.assertEqual(len(self.context.sent), 1)
        self.assertFalse(event.sent)

    async def test_webui_only_failure_does_not_send_chat_notice(self):
        self.image_provider.error = "studio drawing failed"
        env = await self.plugin.dialogue.environment("webui:studio")
        queued = await self.plugin._queue_job(env, {"decision": "photo", "prompt": "portrait", "state_patch": {}, "reply": "照片。"}, "自拍", {})
        await asyncio.gather(*list(self.plugin._tasks))
        self.assertEqual(self.store.job(queued["job_id"])["status"], "failed")
        self.assertFalse(self.context.sent)

    async def test_cancelled_native_photo_does_not_notify_failure(self):
        self.image_provider.pause = asyncio.Event()
        event = Event("看看自拍")
        result = json.loads(await self.plugin.tool_photo(event, "portrait"))
        for _ in range(100):
            if self.image_provider.calls:
                break
            await asyncio.sleep(.001)
        self.assertTrue(self.image_provider.calls)
        task = self.plugin._job_tasks[result["job_id"]]
        await self.plugin.web_cancel_job({"id": result["job_id"]})
        await asyncio.gather(task, return_exceptions=True)
        job = self.store.job(result["job_id"])
        self.assertEqual(job["status"], "cancelled")
        self.assertFalse(job.get("notification_state"))
        self.assertFalse(self.context.sent)
        self.assertFalse(event.sent)

    async def test_rejected_image_delivery_keeps_saved_asset_and_delivery_state(self):
        event = Event("看看自拍")
        event.message_obj = types.SimpleNamespace(message_id="rejected-image-notice")
        async def rejected_image(umo, chain):
            self.context.sent.append((umo, chain))
            return not any(kind == "image" for kind, _ in chain.chain)
        self.context.send_message = rejected_image
        _, job = await self.native(event)
        self.assertEqual(job["status"], "delivery_failed")
        self.assertEqual(job["failure_stage"], "delivery")
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.assertEqual(self.store.delivery(job["delivery_key"])["status"], "delivery_failed")
        self.assertEqual(job["notification_state"], "accepted")
        self.assertEqual(job["notification_channel"], "context")
        self.assertFalse(event.sent)
        self.assertEqual(len(self.context.sent), 2)
        self.assertEqual([umo for umo, _ in self.context.sent], [event.unified_msg_origin] * 2)
        self.assertTrue(any(kind == "image" for kind, _ in self.context.sent[0][1].chain))
        self.assertFalse(any(kind == "image" for kind, _ in self.context.sent[1][1].chain))
        self.assertIn("原图已保留", str(self.context.sent[1][1].chain))
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_local_image_message_preparation_failure_keeps_generation_result(self):
        with patch.object(fixtures.main.MessageChain, "file_image", side_effect=ValueError("local message preparation failed")):
            event, job = await self.native()
        self.assertEqual(job["status"], "delivery_failed")
        self.assertEqual(job["failure_stage"], "delivery")
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.assertEqual(job["notification_state"], "accepted")
        self.assertEqual(job["notification_channel"], "context")
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertFalse(event.sent)
        self.assertEqual(len(self.context.sent), 1)
        self.assertIn("local message preparation failed", str(self.context.sent[0][1].chain))

    async def test_uncertain_image_delivery_keeps_saved_asset_and_uncertainty(self):
        event = Event("看看自拍")
        event.message_obj = types.SimpleNamespace(message_id="uncertain-image-notice")
        async def uncertain_image(umo, chain):
            self.context.sent.append((umo, chain))
            if any(kind == "image" for kind, _ in chain.chain):
                raise ConnectionError("adapter connection lost")
            return True
        self.context.send_message = uncertain_image
        _, job = await self.native(event)
        self.assertEqual(job["status"], "uncertain")
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.assertEqual(self.store.delivery(job["delivery_key"])["status"], "uncertain")
        self.assertEqual(job["notification_state"], "accepted")
        self.assertEqual(job["notification_channel"], "context")
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertFalse(event.sent)
        self.assertEqual(len(self.context.sent), 2)
        self.assertEqual([umo for umo, _ in self.context.sent], [event.unified_msg_origin] * 2)
        self.assertTrue(any(kind == "image" for kind, _ in self.context.sent[0][1].chain))
        self.assertFalse(any(kind == "image" for kind, _ in self.context.sent[1][1].chain))
        self.assertIn("发送结果不确定", str(self.context.sent[1][1].chain))


if __name__ == "__main__":
    unittest.main()
