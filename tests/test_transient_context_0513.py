"""Request-hook regressions using temporary storage and offline SDK doubles."""
import copy
import types
import unittest
from unittest.mock import AsyncMock

import test_core as fixtures
from test_core import Event, TextPart
from canvas_test.companion import apply_state
from canvas_test.intent import DECISION_RULES
import test_tool_failures as confirmation


class TransientContext0513Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    seed_pending = confirmation.GroupConfirmationTests.seed_pending

    def request(self, **updates):
        return types.SimpleNamespace(**{"system_prompt": "AstrBot 原系统提示，包含其他插件的固定规则。", "prompt": "用户原话", "contexts": [{"role": "user", "content": "已有历史"}], "conversation": None, "extra_user_content_parts": [], **updates})

    async def test_preserves_system_prompt_history_user_text_media_and_other_parts(self):
        permanent = TextPart("其他插件的持久内容")
        temporary = TextPart("其他插件的临时内容").mark_as_temp()
        req = self.request(extra_user_content_parts=[permanent, temporary], image_urls=["existing-image"], audio_urls=["existing-audio"], func_tool=object())
        original = {name: getattr(req, name) for name in ("system_prompt", "prompt", "contexts", "image_urls", "audio_urls", "func_tool")}
        history = copy.deepcopy(req.contexts)
        parts = req.extra_user_content_parts
        await self.plugin.inject_visual_state(Event(), req)
        for name, value in original.items():
            self.assertIs(getattr(req, name), value)
        self.assertEqual(req.contexts, history)
        self.assertIs(req.extra_user_content_parts, parts)
        self.assertEqual(req.extra_user_content_parts[:2], [permanent, temporary])
        self.assertFalse(permanent._no_save)
        self.assertTrue(temporary._no_save)
        part = req.extra_user_content_parts[-1]
        self.assertTrue(part._no_save)
        self.assertIn(DECISION_RULES, part.text)
        self.assertIn("request_summary", part.text)
        self.assertIn("本轮用户消息已通过明确图片请求检查", part.text)
        self.assertFalse(self.context.llm.calls)
        self.assertFalse(self.image_provider.calls)

    async def test_repeated_hook_replaces_owned_block_by_identity_and_preserves_list(self):
        req = self.request()
        event = Event("随便聊聊")
        await self.plugin.inject_visual_state(event, req)
        first = req.extra_user_content_parts[-1]
        # Even an identical block from another plugin must not be removed.
        other = TextPart(first.text).mark_as_temp()
        req.extra_user_content_parts.append(other)
        parts = req.extra_user_content_parts
        await self.plugin.inject_visual_state(event, req)
        self.assertIs(req.extra_user_content_parts, parts)
        self.assertEqual(len(parts), 2)
        self.assertIs(parts[0], other)
        self.assertIsNot(parts[1], first)
        self.assertIs(parts[1], req._persona_canvas_visual_part)

    async def test_state_last_image_photos_and_pending_change_without_system_change(self):
        event = Event("陪我聊一会")
        req = self.request()
        await self.plugin.inject_visual_state(event, req)
        old_text, system = req.extra_user_content_parts[-1].text, req.system_prompt
        env = await self.plugin.dialogue.environment(event.unified_msg_origin)
        session = self.store.read_session(env["key"])
        apply_state(session, {"outfit": "new blue dress", "scene": "new garden"}, self.store.settings)
        asset = self.store.save_asset(fixtures.image_bytes(), "png").name
        session["last_image"] = asset
        self.store.save_session(env["key"], session)
        key, pending = await self.seed_pending(event)
        self.store.record_action({"kind": "photo", "session_key": key, "persona_id": env["persona"]["id"], "status": "sent", "summary": "latest-actual-photo", "mode": "persona"})
        await self.plugin.inject_visual_state(event, req)
        text = req.extra_user_content_parts[-1].text
        self.assertNotEqual(text, old_text)
        self.assertEqual(req.system_prompt, system)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        for value in ("new blue dress", "new garden", asset, "latest-actual-photo", pending["request_id"]):
            self.assertIn(value, text)

    async def test_changed_request_and_previous_conditions_stay_available_to_model(self):
        event = Event("给我拍一张蓝裙自拍")
        _, pending = await self.seed_pending(event)
        req = self.request()
        await self.plugin.inject_visual_state(event, req)
        text = req.extra_user_content_parts[-1].text
        self.assertIn('"changed_request": true', text)
        self.assertIn('"previous_conditions":', text)
        self.assertIn(pending["request_id"], text)
        self.assertIn("wide shot", text)

    async def test_conditional_instructions_follow_mode_and_addressing_without_changing_system(self):
        for mode, addressed, explicit in (("native_tools", True, True), ("native_tools", True, False), ("native_tools", False, True), ("compatibility", True, True)):
            with self.subTest(mode=mode, addressed=addressed, explicit=explicit):
                self.store.settings["integration"]["mode"] = mode
                event = Event("看看自拍" if explicit else "聊聊天", "qq:GroupMessage:456")
                event.is_private_chat = lambda: False
                event.is_at_or_wake_command = addressed
                req = self.request()
                system = req.system_prompt
                await self.plugin.inject_visual_state(event, req)
                text = req.extra_user_content_parts[-1].text
                self.assertEqual(req.system_prompt, system)
                self.assertEqual("原生工具支持上下文语义判断" in text, mode == "native_tools" and addressed)
                self.assertEqual("本轮用户消息已通过明确图片请求检查" in text, mode == "native_tools" and addressed and explicit)

    async def test_disabling_integration_removes_only_our_existing_block(self):
        other = TextPart("other")
        req = self.request(extra_user_content_parts=[other])
        await self.plugin.inject_visual_state(Event(), req)
        self.store.settings["integration"]["enabled"] = False
        await self.plugin.inject_visual_state(Event(), req)
        self.assertEqual(req.extra_user_content_parts, [other])
        self.assertEqual(req.system_prompt, "AstrBot 原系统提示，包含其他插件的固定规则。")

    async def test_disabled_integration_does_not_add_a_content_field(self):
        self.store.settings["integration"]["enabled"] = False
        req = types.SimpleNamespace(system_prompt=None)
        await self.plugin.inject_visual_state(Event(), req)
        self.assertIsNone(req.system_prompt)
        self.assertFalse(hasattr(req, "extra_user_content_parts"))

    async def test_empty_or_missing_parts_create_a_fresh_temporary_block(self):
        for fields in ({}, {"extra_user_content_parts": None}):
            with self.subTest(fields=fields):
                req = types.SimpleNamespace(system_prompt=None, **fields)
                await self.plugin.inject_visual_state(Event(), req)
                self.assertIsNone(req.system_prompt)
                self.assertEqual(len(req.extra_user_content_parts), 1)
                self.assertTrue(req.extra_user_content_parts[0]._no_save)

    async def test_other_plugin_removing_the_parts_list_does_not_restore_stale_state(self):
        req = self.request()
        await self.plugin.inject_visual_state(Event(), req)
        old = req._persona_canvas_visual_part
        req.extra_user_content_parts = None
        await self.plugin.inject_visual_state(Event(), req)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIsNot(req.extra_user_content_parts[0], old)

    async def test_failing_environment_clears_owned_block_without_rewriting_system(self):
        other = TextPart("other")
        req = self.request(extra_user_content_parts=[other])
        await self.plugin.inject_visual_state(Event(), req)
        self.plugin.dialogue.environment = AsyncMock(side_effect=ValueError("unavailable"))
        with self.assertRaises(ValueError):
            await self.plugin.inject_visual_state(Event(), req)
        self.assertEqual(req.extra_user_content_parts, [other])
        self.assertEqual(req.system_prompt, "AstrBot 原系统提示，包含其他插件的固定规则。")

    async def test_plugin_hook_does_not_append_synthetic_history_pairs(self):
        event = Event()
        history = copy.deepcopy(self.context.conversation_manager.history)
        self.context.conversation_manager.add_message_pair = AsyncMock()
        req = self.request()
        await self.plugin.inject_visual_state(event, req)
        self.assertEqual(self.context.conversation_manager.history, history)
        self.context.conversation_manager.add_message_pair.assert_not_awaited()

