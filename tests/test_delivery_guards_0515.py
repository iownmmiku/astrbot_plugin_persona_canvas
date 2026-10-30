"""Offline regressions for changed agreements and unverified photo delivery."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock

import test_core as fixtures
import test_webchat_delivery_0511 as webchat


class AgreementReplacement0515Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    async def pending_photo(self, *, umo="qq:FriendMessage:123"):
        event = fixtures.Event("给我拍一张泳装自拍", umo)
        env = await self.plugin._env(event)
        pending = self.plugin._save_conditions(
            env, event.message_str, "泳装远景，可以吗？",
            {"outfit": "swimsuit", "camera": "wide shot"},
        )
        return env, pending

    async def test_replacing_executing_conditions_cancels_only_the_associated_photo(self):
        env, pending = await self.pending_photo()
        self.image_provider.pause = asyncio.Event()
        confirm = fixtures.Event("就按你说的拍")
        result = json.loads(await self.plugin.tool_photo(
            confirm, "swimsuit photo", request_summary="用户确认泳装远景",
            confirmed_request_id=pending["request_id"],
        ))
        self.assertTrue(result["ok"], result)
        for _ in range(200):
            if self.image_provider.calls:
                break
            await asyncio.sleep(.001)
        self.assertTrue(self.image_provider.calls)
        original_task = self.plugin._job_tasks[result["job_id"]]
        unrelated = self.store.create_job({
            "status": "queued", "source": "chat", "mode": "persona",
            "umo": "qq:FriendMessage:other", "session_key": "other-session",
            "persona_id": env["persona"]["id"],
        })

        changed = fixtures.Event("改成外套再拍")
        update = json.loads(await self.plugin.tool_conditions(
            changed, "改成外套再拍，可以吗？", outfit="coat",
            request_summary="用户把泳装更换成外套，需要重新确认",
        ))
        self.assertTrue(update["ok"], update)
        self.image_provider.pause.set()
        await asyncio.gather(original_task, return_exceptions=True)

        original = self.store.job(result["job_id"])
        self.assertEqual(original["status"], "cancelled")
        self.assertTrue(original["cancel_requested"])
        self.assertFalse(self.context.sent)
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(self.store.job(unrelated["id"])["status"], "queued")
        revised = self.store.read_session(env["key"])["pending"]
        self.assertNotEqual(revised["request_id"], pending["request_id"])
        self.assertEqual(revised["requirements"]["outfit"], "coat")
        self.assertEqual(revised["requirements"]["camera"], "wide shot")
        self.assertFalse(revised.get("execution_job_id"))

    async def test_replacing_conditions_cannot_cancel_a_job_from_another_session(self):
        env, pending = await self.pending_photo()
        other = self.store.create_job({
            "status": "queued", "session_key": "other-session",
            "persona_id": env["persona"]["id"],
        })
        session = self.store.read_session(env["key"])
        session["pending"]["execution_job_id"] = other["id"]
        self.store.save_session(env["key"], session)

        result = json.loads(await self.plugin.tool_conditions(
            fixtures.Event("改成外套再拍"), "改成外套，可以吗？", outfit="coat",
            request_summary="用户修改要求，需要重新确认",
        ))

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.store.job(other["id"])["status"], "queued")


class CompatibilityReceipt0515Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = webchat.WebChatDelivery0511Tests.asyncSetUp
    asyncTearDown = webchat.WebChatDelivery0511Tests.asyncTearDown

    @staticmethod
    def photo_decision():
        return {"decision": "photo", "reply": "给你看看。", "prompt": "portrait", "state_patch": {}}

    async def test_unverified_result_does_not_report_sent_or_write_photo_memory(self):
        event = webchat.SimulatedWebChatEvent(self.platform)
        env = await self.plugin._env(event)
        before = copy.deepcopy(self.context.conversation_manager.history)
        result = await self.plugin._apply_decision(
            env, self.photo_decision(), event.message_str, event=event, explicit=True,
        )

        self.assertFalse(result["sent"])
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["delivery_evidence"], "unverified")
        self.assertEqual(self.store.job(result["job_id"])["status"], "uncertain")
        self.assertEqual(self.context.conversation_manager.history, before)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_command_and_compatibility_route_preserve_unverified_outcome(self):
        for route in ("command", "compatibility"):
            with self.subTest(route=route):
                self.context.llm.next = self.photo_decision()
                self.plugin.dialogue.remember = AsyncMock()
                event = webchat.SimulatedWebChatEvent(self.platform, message_id=route)
                if route == "command":
                    results = [item async for item in self.plugin.command_generate(event, "看看自拍")]
                else:
                    self.store.settings["integration"]["mode"] = "compatibility"
                    results = [item async for item in self.plugin.natural_route(event)]
                self.assertFalse(results)
                self.plugin.dialogue.remember.assert_not_awaited()
                self.assertEqual(self.store.recent_jobs()[0]["status"], "uncertain")
        self.assertEqual(len(self.context.sent), 2)
        self.assertEqual(len(self.image_provider.calls), 2)

    async def test_normal_platform_still_records_completed_send(self):
        event = fixtures.Event("看看自拍")
        env = await self.plugin._env(event)
        self.plugin.dialogue.remember = AsyncMock()
        result = await self.plugin._apply_decision(
            env, self.photo_decision(), event.message_str, event=event, explicit=True,
        )

        self.assertTrue(result["sent"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["delivery_evidence"], "call_completed")
        self.assertEqual(len(event.sent), 1)
        self.plugin.dialogue.remember.assert_awaited_once()
        self.assertIn("[已发送照片：", self.plugin.dialogue.remember.await_args.args[2])


if __name__ == "__main__":
    unittest.main()
