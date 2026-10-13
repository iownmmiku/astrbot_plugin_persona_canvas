"""Keep the WebChat send outcome when platform lookup changes mid-send."""
import asyncio
import unittest
from unittest.mock import AsyncMock

import test_webchat_delivery_0511 as webchat


class WebChatActiveReceipt0511Tests(unittest.IsolatedAsyncioTestCase):
    # Reuse setup helpers without importing/discovering the original test class.
    asyncSetUp = webchat.WebChatDelivery0511Tests.asyncSetUp
    asyncTearDown = webchat.WebChatDelivery0511Tests.asyncTearDown
    setup_webchat_morning = webchat.WebChatDelivery0511Tests.setup_webchat_morning
    assert_morning_not_automatically_retried = webchat.WebChatDelivery0511Tests.assert_morning_not_automatically_retried

    def remove_platform_after_send(self):
        original_send = self.context.send_message

        async def send_then_remove(umo, chain):
            result = await original_send(umo, chain)
            # A platform can be unloaded during the awaited send. The old
            # target deliberately has no persisted platform-type metadata.
            self.context.get_platform_inst = lambda _platform_id: None
            await asyncio.sleep(0)
            return result

        self.context.send_message = send_then_remove
        self.plugin.dialogue.remember = AsyncMock()

    async def test_photo_retains_uncertain_when_lookup_disappears_after_send(self):
        scheduler, target, key = self.setup_webchat_morning(with_image=True)
        target.pop("platform_type", None)
        target.pop("platform_name", None)
        self.store.save_targets()
        self.context.llm.next = {"decision": "photo", "reply": "早安。", "prompt": "morning portrait", "state_patch": {}}
        self.remove_platform_after_send()

        await scheduler.tick(wait=True)

        delivery = self.store.delivery(key)
        job = self.store.job(delivery["job_id"])
        self.assertEqual(job["status"], "uncertain")
        self.assertEqual(job["delivery_platform"], "webchat")
        self.assertEqual(job["delivery_evidence"], "unverified")
        self.assertEqual(delivery["status"], "uncertain")
        rows = self.platform.history(webchat.UMO.split(":", 2)[2])
        self.assertEqual(len(rows), 1)
        self.assertTrue(any(part[0] == "image" for row in rows for part in row))
        self.assertEqual(len(self.image_provider.calls), 1)
        self.plugin.dialogue.remember.assert_not_awaited()
        await self.assert_morning_not_automatically_retried(scheduler, target, key)

    async def test_text_retains_uncertain_when_lookup_disappears_after_send(self):
        scheduler, target, key = self.setup_webchat_morning(with_image=False)
        target.pop("platform_type", None)
        target.pop("platform_name", None)
        self.store.save_targets()
        self.context.llm.next = {"decision": "chat", "reply": "早安，今天也一起好好生活吧。"}
        self.remove_platform_after_send()

        await scheduler.tick(wait=True)

        delivery = self.store.delivery(key)
        self.assertEqual(delivery["status"], "uncertain")
        self.assertEqual(delivery["delivery_evidence"], "unverified")
        rows = self.platform.history(webchat.UMO.split(":", 2)[2])
        self.assertEqual(len(rows), 1)
        self.assertTrue(any(part[0] == "text" and "早安" in part[1] for row in rows for part in row))
        self.assertFalse(self.image_provider.calls)
        self.plugin.dialogue.remember.assert_not_awaited()
        await self.assert_morning_not_automatically_retried(scheduler, target, key)


if __name__ == "__main__":
    unittest.main()
