"""Additional offline cases found during an independent runtime review."""
import asyncio
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_core import Context, Event, ImageProvider, Storage, main
from canvas_test import dialogue as dialogue_module
from canvas_test.intent import photo_request, state_request


class RuntimeReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = Context()
        self.store = Storage(Path(self.temp.name))
        with patch.object(main, "Storage", return_value=self.store):
            self.plugin = main.PersonaCanvasPlugin(self.context, {})
        self.image_provider = ImageProvider()
        self.plugin._provider = lambda name=None: self.image_provider
        self.store.settings["moderation"]["min_interval_sec"] = 0
        self.native_task = None

    async def asyncTearDown(self):
        if self.native_task and not self.native_task.done():
            self.native_task.cancel()
            await asyncio.gather(self.native_task, return_exceptions=True)
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_state_only_condition_does_not_grant_photo_confirmation(self):
        event = Event("换成白色连衣裙")
        env = await self.plugin.dialogue.environment(event.unified_msg_origin)
        await self.plugin._apply_decision(env, {"decision": "ask", "reply": "换白裙可以吗？", "state_patch": {}}, event.message_str, event=event)
        current = await self.plugin.dialogue.environment(event.unified_msg_origin)
        self.assertFalse(self.plugin._eligible("好", current))
        self.assertFalse(self.image_provider.calls)

    async def test_no_persona_sentinel_does_not_crash_or_inject_default_persona(self):
        self.context.conversation_manager.get_conversation = AsyncMock(return_value=types.SimpleNamespace(cid="no-persona", persona_id="[%None]", history="[]"))
        self.context.persona_manager.get_persona = AsyncMock(side_effect=ValueError("Persona does not exist"))
        self.context.persona_manager.get_default_persona_v3 = AsyncMock(return_value={"name": "unwanted", "prompt": "must not inject this persona"})
        env = await self.plugin.dialogue.environment("qq:FriendMessage:123")
        self.assertNotIn("must not inject", env["system"])
        self.context.persona_manager.get_persona.assert_not_awaited()
        self.context.persona_manager.get_default_persona_v3.assert_not_awaited()

    async def test_session_service_persona_override_matches_astrbot_selection(self):
        for persona_id in ["session-specific-role", "[%None]"]:
            with self.subTest(persona_id=persona_id):
                service = types.SimpleNamespace(get_async=AsyncMock(return_value={"persona_id": persona_id}))
                with patch.object(sys.modules["astrbot.api"], "sp", service, create=True), patch.object(dialogue_module, "sp", service, create=True):
                    env = await self.plugin.dialogue.environment("qq:FriendMessage:123")
                self.assertEqual(env["astro_id"], persona_id)
                if persona_id == "[%None]":
                    self.assertEqual(env["system"], "")

    async def test_unrelated_chat_does_not_leave_short_confirmation_armed(self):
        request = Event("给我拍一张你的自拍")
        await self.plugin.remember_conditions(request, types.SimpleNamespace(completion_text="只拍背影可以吗？"))
        unrelated = Event("你今天吃的什么？")
        await self.plugin.remember_conditions(unrelated, types.SimpleNamespace(completion_text="今天吃了面条。"))
        current = await self.plugin.dialogue.environment(unrelated.unified_msg_origin)
        self.assertFalse(self.plugin._eligible("好", current))

    async def test_restart_recovers_pre_send_delivery_for_retry(self):
        self.store.reserve_delivery("morning:qq:FriendMessage:123:today")
        self.store.update_delivery("morning:qq:FriendMessage:123:today", {"status": "generating", "kind": "morning", "attempts": 1})
        self.store.reserve_delivery("morning:qq:FriendMessage:456:today")
        self.store.update_delivery("morning:qq:FriendMessage:456:today", {"status": "sending", "kind": "morning", "attempts": 1})
        await self.plugin.initialize()
        self.assertEqual(self.store.delivery("morning:qq:FriendMessage:123:today")["status"], "failed")
        self.assertEqual(self.store.delivery("morning:qq:FriendMessage:456:today")["status"], "uncertain")

    async def test_terminate_cancels_native_tool_generation_before_storage_close(self):
        self.image_provider.pause = asyncio.Event()
        result = json.loads(await self.plugin.tool_photo(Event("给我拍一张自拍"), "blue coat"))
        self.assertEqual(result["status"], "queued")
        self.assertFalse(result["image_sent"])
        self.native_task = next(task for task in self.plugin._tasks if task.get_name() == "persona-canvas-chat-photo")
        for _ in range(100):
            if self.image_provider.calls:
                break
            await asyncio.sleep(0)
        self.assertTrue(self.image_provider.calls)
        await self.plugin.terminate()
        self.assertTrue(self.native_task.done(), "Reload must finish/cancel native generation before closing storage")
        self.assertTrue(self.native_task.cancelled())
        self.assertTrue(self.store._closed)
        reopened = Storage(Path(self.temp.name))
        try:
            self.assertEqual(reopened.job(result["job_id"])["status"], "cancelled")
        finally:
            reopened.close()

    async def test_command_and_compatibility_route_share_one_request_claim(self):
        self.store.settings["integration"]["mode"] = "compatibility"
        self.context.llm.next = {"decision": "photo", "reply": "我拍一张吧。", "prompt": "blue coat"}
        for order in [("command", "natural"), ("natural", "command")]:
            with self.subTest(order=order):
                image_count, llm_count = len(self.image_provider.calls), len(self.context.llm.calls)
                event = Event("拍照 给我拍一张自拍")
                for route in order:
                    handler = self.plugin.command_generate(event, "给我拍一张自拍") if route == "command" else self.plugin.natural_route(event)
                    async for _ in handler:
                        pass
                self.assertEqual(len(self.image_provider.calls) - image_count, 1)
                self.assertEqual(len(self.context.llm.calls) - llm_count, 1)


class EligibilityReviewTests(unittest.TestCase):
    def test_state_narration_and_quotation_are_not_role_change_requests(self):
        for text in ["我坐在窗边看书", "她说换成白裙", "比如‘换成白色连衣裙’", "假如你穿上白裙会怎样", "昨天我换上了新衣服"]:
            with self.subTest(text=text):
                self.assertFalse(state_request(text))
        for text in ["换成白色连衣裙", "你坐在窗边", "请穿上白裙"]:
            self.assertTrue(state_request(text), text)

    def test_photo_process_questions_are_not_picture_requests(self):
        for text in ["给我讲解生成图片的过程", "给我讲讲你拍照片的习惯", "我想看图片生成教程"]:
            with self.subTest(text=text):
                self.assertFalse(photo_request(text))
        self.assertFalse(photo_request("好", {"request": "换成白裙", "kind": "state", "expires_at": time.time() + 60}))


if __name__ == "__main__":
    unittest.main()
