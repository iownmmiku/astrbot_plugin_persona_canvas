"""Offline regressions for the reported group-chat photo confirmation chain.

These tests use temporary SQLite storage and fake AstrBot, LLM and image IO.
"""
import asyncio
import copy
import json
import time
import types
import unittest

import test_core as fixtures
from test_core import Event
from canvas_test.companion import pending_request
from canvas_test.intent import confirmation_text, photo_request


class GroupConfirmationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    consume = fixtures.RuntimeTests.consume

    def group_event(self, text, umo="qq:GroupMessage:123:456"):
        event = Event(text, umo)
        event.is_private_chat = lambda: False
        event.is_at_or_wake_command = True
        return event

    async def request_hook(self, event):
        req = types.SimpleNamespace(system_prompt="原来的 AstrBot 人格", conversation=None)
        await self.plugin.inject_visual_state(event, req)
        return req

    async def seed_pending(self, event, *, kind="photo"):
        env = await self.plugin.dialogue.environment(event.unified_msg_origin)
        session = self.store.session(env["key"], env["persona"]["id"])
        session["pending"] = pending_request(
            env,
            "看一下你的自拍",
            "穿白裙、拍远一点，不拍近景，听懂了吗？",
            {"outfit": "white dress", "camera": "wide shot", "avoid": "close-up"},
            kind=kind,
        )
        self.store.save_session(env["key"], session)
        return env["key"], session["pending"]

    async def settle_jobs(self):
        tasks = list(self.plugin._tasks)
        if tasks:
            await asyncio.gather(*tasks)

    def test_transport_prefixes_do_not_hide_explicit_selfie_request(self):
        for prefix in (
            "[At:3993931121] ",
            "[CQ:at,qq=3993931121] ",
            "[CQ:reply,id=2468] [CQ:at,qq=3993931121] ",
        ):
            with self.subTest(prefix=prefix):
                self.assertTrue(photo_request(prefix + "看一下你的自拍"))
                self.assertFalse(photo_request(prefix + "“看一下你的自拍”"))
                self.assertFalse(photo_request(prefix + "```看一下你的自拍```"))
                self.assertFalse(photo_request(prefix + "不要给我发自拍"))

    async def test_reported_group_request_conditions_and_short_confirmation_send_photo(self):
        # Independent wording variants share the fixture sender; this test
        # exercises confirmation, not the separate five-images daily quota.
        self.store.settings["moderation"]["daily_limit"] = 0
        scenarios = [(prefix, confirmation) for prefix in ("", "[At:3993931121] ") for confirmation in ("我要看", "听懂了", "发吧", "可以，发吧", "我要看，听懂了", "听懂了，发吧")]
        for index, (prefix, confirmation) in enumerate(scenarios):
            with self.subTest(prefix=prefix, confirmation=confirmation):
                umo = f"qq:GroupMessage:123:{1000 + index}"
                previous_calls = len(self.image_provider.calls)
                first = self.group_event(prefix + "看一下你的自拍", umo)
                await self.request_hook(first)
                accepted = json.loads(await self.plugin.tool_conditions(
                    first,
                    "穿白裙、拍远一点，不拍近景，听懂了吗？",
                    outfit="white dress", camera="wide shot", avoid="close-up",
                ))
                self.assertTrue(accepted["ok"])
                self.assertEqual(len(self.image_provider.calls), previous_calls)
                env = await self.plugin._env(first)
                request_id = self.store.session(env["key"])["pending"]["request_id"]

                followup = self.group_event(prefix + confirmation, umo)
                await self.request_hook(followup)
                self.assertEqual(self.store.session(env["key"])["pending"].get("request_id"), request_id)
                queued = json.loads(await self.plugin.tool_photo(
                    followup, "portrait, white dress, wide shot", caption="那给你看看。",
                ))
                self.assertTrue(queued["ok"], queued)
                await self.settle_jobs()
                job = self.store.job(queued["job_id"])
                self.assertEqual(job["status"], "sent")
                self.assertEqual(job["requirements"]["camera"], "wide shot")
                self.assertEqual(job["requirements"]["avoid"], "close-up")
                self.assertIn("wide shot", self.image_provider.calls[-1]["positive"])
                self.assertIn("close-up", self.image_provider.calls[-1]["positive"])
                self.assertFalse(self.store.session(env["key"])["pending"])

    async def test_cq_reply_and_at_headers_preserve_pending_confirmation(self):
        for index, prefix in enumerate((
            "[CQ:at,qq=3993931121] ",
            "[CQ:reply,id=2468] [CQ:at,qq=3993931121] ",
        )):
            with self.subTest(prefix=prefix):
                event = self.group_event(prefix + "发吧", f"qq:GroupMessage:123:cq{index}")
                key, pending = await self.seed_pending(event)
                await self.request_hook(event)
                self.assertEqual(self.store.session(key)["pending"].get("request_id"), pending["request_id"])
                queued = json.loads(await self.plugin.tool_photo(event, "portrait, wide shot"))
                self.assertTrue(queued["ok"], queued)
                await self.settle_jobs()
                self.assertEqual(self.store.job(queued["job_id"])["status"], "sent")

    async def test_compatibility_mode_uses_same_short_confirmation_and_conditions(self):
        self.store.settings["integration"]["mode"] = "compatibility"
        event = self.group_event("[At:3993931121] 听懂了")
        key, _pending = await self.seed_pending(event)
        self.context.llm.next = {"decision": "photo", "reply": "那就给你看啦。", "prompt": "portrait", "state_patch": {}}
        await self.consume(self.plugin.natural_route(event))
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(self.store.recent_jobs()[0]["status"], "sent")
        self.assertEqual(self.store.recent_jobs()[0]["requirements"]["camera"], "wide shot")
        self.assertFalse(self.store.session(key)["pending"])

    async def test_short_confirmations_without_pending_cannot_authorize_photo(self):
        for confirmation in ("我要看", "听懂了", "发吧"):
            with self.subTest(confirmation=confirmation):
                event = self.group_event("[At:3993931121] " + confirmation)
                await self.request_hook(event)
                result = json.loads(await self.plugin.tool_photo(event, "portrait"))
                self.assertFalse(result["ok"], result)
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.store.recent_jobs())

    async def test_expired_pending_cannot_authorize_short_confirmation(self):
        event = self.group_event("[At:3993931121] 我要看")
        key, _pending = await self.seed_pending(event)
        session = self.store.session(key)
        session["pending"]["expires_at"] = time.time() - 1
        self.store.save_session(key, session)
        await self.request_hook(event)
        self.assertFalse(json.loads(await self.plugin.tool_photo(event, "portrait"))["ok"])
        self.assertFalse(self.image_provider.calls)

    async def test_pending_from_other_visual_persona_cannot_authorize_confirmation(self):
        event = self.group_event("[At:3993931121] 听懂了")
        await self.seed_pending(event)
        other = copy.deepcopy(self.store.persona())
        other.update(id="other-role", name="另一个角色")
        self.store.upsert_persona(other)
        self.store.settings["current_persona"] = other["id"]
        await self.request_hook(event)
        self.assertFalse(json.loads(await self.plugin.tool_photo(event, "portrait"))["ok"])
        self.assertFalse(self.image_provider.calls)

    async def test_pending_from_other_conversation_cannot_authorize_confirmation(self):
        event = self.group_event("[At:3993931121] 发吧")
        original_key, pending = await self.seed_pending(event)
        self.context.conversation_manager.cid = "conversation-2"
        await self.request_hook(event)
        self.assertFalse(json.loads(await self.plugin.tool_photo(event, "portrait"))["ok"])
        self.assertEqual(self.store.session(original_key)["pending"]["request_id"], pending["request_id"])
        self.assertFalse(self.image_provider.calls)

    async def test_state_confirmation_cannot_authorize_photo(self):
        event = self.group_event("[At:3993931121] 听懂了")
        await self.seed_pending(event, kind="state")
        await self.request_hook(event)
        self.assertFalse(json.loads(await self.plugin.tool_photo(event, "portrait"))["ok"])
        self.assertFalse(self.image_provider.calls)

    async def test_photo_only_acknowledgements_cannot_update_state_conditions(self):
        for index, acknowledgement in enumerate(("我要看", "发吧")):
            with self.subTest(acknowledgement=acknowledgement):
                event = self.group_event(acknowledgement, f"qq:GroupMessage:123:state{index}")
                key, _pending = await self.seed_pending(event, kind="state")
                before = copy.deepcopy(self.store.session(key)["state"])
                await self.request_hook(event)
                updated = json.loads(await self.plugin.tool_state(event, outfit="swimsuit"))
                self.assertFalse(updated["ok"], updated)
                self.assertFalse(json.loads(await self.plugin.tool_photo(event, "portrait"))["ok"])
                self.assertEqual(self.store.session(key)["state"], before)
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.store.recent_jobs())

    async def test_understood_state_conditions_only_update_agreed_state(self):
        event = self.group_event("听懂了")
        key, _pending = await self.seed_pending(event, kind="state")
        await self.request_hook(event)
        updated = json.loads(await self.plugin.tool_state(event, outfit="swimsuit"))
        self.assertTrue(updated["ok"], updated)
        self.assertFalse(updated["photo_generated"])
        self.assertEqual(self.store.session(key)["state"]["outfit"], "white dress")
        self.assertFalse(self.store.session(key)["pending"])
        self.assertFalse(json.loads(await self.plugin.tool_photo(event, "portrait"))["ok"])
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.store.recent_jobs())

    async def test_negation_quoted_code_and_narrated_acknowledgements_are_not_consent(self):
        texts = (
            "不要发吧", "我不想看了", "我没听懂", "好，不要拍了",
            "“我要看”", "'发吧'", "```听懂了```", "```\n发吧\n```",
            "她说“发吧”", "他说我要看", "我刚才说听懂了",
            "假如我说发吧", "听懂了是什么意思？",
        )
        for index, text in enumerate(texts):
            with self.subTest(text=text):
                event = self.group_event("[At:3993931121] " + text, f"qq:GroupMessage:123:ordinary{index}")
                key, _pending = await self.seed_pending(event)
                before = copy.deepcopy(self.store.session(key)["state"])
                self.assertFalse(confirmation_text(event.message_str))
                await self.request_hook(event)
                self.assertFalse(json.loads(await self.plugin.tool_photo(event, "portrait"))["ok"])
                self.assertFalse(json.loads(await self.plugin.tool_state(event, outfit="swimsuit"))["ok"])
                self.assertEqual(self.store.session(key)["state"], before)
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.store.recent_jobs())

    async def test_other_person_photo_request_is_not_the_users_consent(self):
        for index, text in enumerate(("她说给我拍张自拍", "他说我想看你的自拍", "刚才她说帮我拍张照片")):
            with self.subTest(text=text):
                event = self.group_event(text, f"qq:GroupMessage:123:narration{index}")
                await self.seed_pending(event)
                await self.request_hook(event)
                self.assertFalse(json.loads(await self.plugin.tool_photo(event, "portrait"))["ok"])
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.store.recent_jobs())

    async def test_mixed_acknowledgement_and_changed_requirements_do_not_confirm(self):
        for index, text in enumerate(("好，但换上泳装", "好，镜头近一点", "听懂了，不过镜头改近一点")):
            with self.subTest(text=text):
                event = self.group_event(text, f"qq:GroupMessage:123:changed{index}")
                key, pending = await self.seed_pending(event)
                before = copy.deepcopy(self.store.session(key)["state"])
                self.assertFalse(confirmation_text(text))
                await self.request_hook(event)
                self.assertFalse(json.loads(await self.plugin.tool_photo(event, "portrait, swimsuit, close-up"))["ok"])
                self.assertFalse(json.loads(await self.plugin.tool_state(event, outfit="swimsuit"))["ok"])
                self.assertEqual(self.store.session(key)["state"], before)
                self.assertEqual(self.store.session(key)["pending"].get("request_id"), pending["request_id"])
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.store.recent_jobs())

    async def test_unaddressed_group_chat_cannot_execute_photo_tool(self):
        for index, text in enumerate(("看一下你的自拍", "听懂了")):
            with self.subTest(text=text):
                event = self.group_event(text, f"qq:GroupMessage:123:unaddressed{index}")
                event.is_at_or_wake_command = False
                if text == "听懂了":
                    await self.seed_pending(event)
                await self.request_hook(event)
                denied = json.loads(await self.plugin.tool_photo(event, "portrait"))
                self.assertFalse(denied["ok"], denied)
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.store.recent_jobs())

    async def test_withdrawal_with_at_header_cancels_pending_and_blocks_later_confirmation(self):
        event = self.group_event("[At:3993931121] 别拍了")
        key, _pending = await self.seed_pending(event)
        replies = await self.consume(self.plugin.cancel_photo(event))
        self.assertTrue(replies)
        self.assertFalse(self.store.session(key)["pending"])
        later = self.group_event("[At:3993931121] 发吧")
        await self.request_hook(later)
        self.assertFalse(json.loads(await self.plugin.tool_photo(later, "portrait"))["ok"])
        self.assertFalse(self.image_provider.calls)

    async def test_changed_requirement_needs_new_confirmation(self):
        event = self.group_event("[At:3993931121] 我要看，镜头改近一点")
        key, pending = await self.seed_pending(event)
        await self.request_hook(event)
        result = json.loads(await self.plugin.tool_photo(event, "portrait, close-up"))
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.store.session(key)["pending"].get("request_id"), pending["request_id"])
        self.assertEqual(self.store.session(key)["pending"]["requirements"]["camera"], "wide shot")
        self.assertFalse(self.image_provider.calls)

    async def test_rejected_tool_in_conditions_turn_does_not_clear_pending(self):
        event = self.group_event("[At:3993931121] 看一下你的自拍")
        await self.request_hook(event)
        accepted = json.loads(await self.plugin.tool_conditions(event, "只拍远景，听懂了吗？", camera="wide shot"))
        self.assertTrue(accepted["ok"])
        env = await self.plugin._env(event)
        request_id = self.store.session(env["key"])["pending"]["request_id"]
        denied = json.loads(await self.plugin.tool_photo(event, "portrait"))
        self.assertFalse(denied["ok"])
        await self.plugin.remember_conditions(event, types.SimpleNamespace(completion_text="还需要你确认。"))
        self.assertEqual(self.store.session(env["key"])["pending"].get("request_id"), request_id)
        self.assertFalse(self.image_provider.calls)

    async def test_invalid_tool_call_preserves_confirmed_conditions_for_new_request(self):
        event = self.group_event("[At:3993931121] 听懂了")
        key, pending = await self.seed_pending(event)
        await self.request_hook(event)
        denied = json.loads(await self.plugin.tool_photo(event, "", mode="unknown"))
        self.assertFalse(denied["ok"])
        await self.plugin.remember_conditions(event, types.SimpleNamespace(completion_text="工具这次没有执行成功。"))
        self.assertEqual(self.store.session(key)["pending"].get("request_id"), pending["request_id"])
        followup = self.group_event("[At:3993931121] 我要看")
        await self.request_hook(followup)
        queued = json.loads(await self.plugin.tool_photo(followup, "portrait, wide shot"))
        self.assertTrue(queued["ok"], queued)
        await self.settle_jobs()
        self.assertEqual(self.store.job(queued["job_id"])["requirements"]["camera"], "wide shot")


if __name__ == "__main__":
    unittest.main()
