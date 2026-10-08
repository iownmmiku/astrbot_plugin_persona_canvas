"""Offline native-tool tests for context intent without positive keyword gates."""
import copy
import json
import time
import types
import unittest

import test_core as fixtures
import test_tool_failures as group_fixtures
from test_core import Event
from canvas_test.intent import photo_request, state_request


class SemanticToolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    consume = fixtures.RuntimeTests.consume
    group_event = group_fixtures.GroupConfirmationTests.group_event
    request_hook = group_fixtures.GroupConfirmationTests.request_hook
    seed_pending = group_fixtures.GroupConfirmationTests.seed_pending
    settle_jobs = group_fixtures.GroupConfirmationTests.settle_jobs

    async def test_context_request_without_keywords_sends_one_photo(self):
        for index, event in enumerate((Event("让我见识一下今天这身打扮"), self.group_event("[At:3993931121] 让我见识一下今天这身打扮"))):
            with self.subTest(index=index):
                self.assertFalse(photo_request(event.message_str))
                req = await self.request_hook(event)
                self.assertIn("request_summary", req.system_prompt)
                result = json.loads(await self.plugin.tool_photo(event, "sailor uniform, cheerful portrait", request_summary="用户接续自拍话题，要看今天服装的实际图片；我愿意展示。"))
                self.assertTrue(result["ok"], result)
                repeated = json.loads(await self.plugin.tool_photo(event, "again", request_summary="同一次请求"))
                self.assertFalse(repeated["ok"])
                await self.settle_jobs()
                job = self.store.job(result["job_id"])
                self.assertEqual(job["status"], "sent")
                self.assertEqual(job["request_basis"]["source"], "model_tool")
                self.assertIn("语义判断", job["trace"][0]["detail"])
                self.assertTrue(event.sent)
        self.assertEqual(len(self.image_provider.calls), 2)
        self.assertFalse(self.context.llm.calls, "Native tools must not spend an extra classification call")

    async def test_unknown_confirmation_keeps_pending_until_model_and_enforces_it(self):
        event = self.group_event("[At:3993931121] 约法三章我都记住了，就依你的安排")
        key, pending = await self.seed_pending(event)
        self.assertFalse(photo_request(event.message_str, pending))
        req = await self.request_hook(event)
        self.assertIn(pending["request_id"], req.system_prompt)
        self.assertEqual(self.store.session(key)["pending"]["request_id"], pending["request_id"])
        result = json.loads(await self.plugin.tool_photo(event, "portrait", outfit="swimsuit", request_summary="用户确认原来的三项约定，没有提出修改；我同意拍照。", confirmed_request_id=pending["request_id"]))
        self.assertTrue(result["ok"], result)
        await self.settle_jobs()
        job = self.store.job(result["job_id"])
        self.assertEqual(job["status"], "sent")
        self.assertEqual(job["state_patch"]["outfit"], "white dress")
        self.assertEqual(job["requirements"]["camera"], "wide shot")
        self.assertNotIn("close-up", self.image_provider.calls[0]["positive"])
        self.assertIn("close-up", self.image_provider.calls[0]["negative"])
        self.assertFalse(self.store.session(key)["pending"])

    async def test_semantic_conditions_do_not_generate_in_the_asking_turn(self):
        event = Event("让我见识一下今天这身打扮")
        await self.request_hook(event)
        result = json.loads(await self.plugin.tool_conditions(event, "可以给你看，但要白裙远景，可以吗？", outfit="white dress", camera="wide shot", request_summary="用户在上下文中请求今天的照片，我先提出条件。"))
        self.assertTrue(result["ok"])
        self.assertFalse(json.loads(await self.plugin.tool_photo(event, "portrait", request_summary="用户想看", confirmed_request_id=result["request_id"]))["ok"])
        self.assertFalse(self.store.recent_jobs())
        self.assertFalse(self.image_provider.calls)

    async def test_repeating_agreed_outfit_is_confirmation_not_a_change(self):
        event = Event("我全部同意，按约定穿上白裙就好")
        _key, pending = await self.seed_pending(event)
        await self.request_hook(event)
        result = json.loads(await self.plugin.tool_photo(event, "white dress, wide shot", request_summary="用户确认白裙和其他原约定，并未修改", confirmed_request_id=pending["request_id"]))
        self.assertTrue(result["ok"], result)
        await self.settle_jobs()
        self.assertEqual(self.store.job(result["job_id"])["status"], "sent")

    async def test_pending_conditions_need_exact_id_for_semantic_confirmation(self):
        event = Event("你的约定我全部接受")
        key, pending = await self.seed_pending(event)
        for request_id in ("", "invented-id"):
            with self.subTest(request_id=request_id):
                result = json.loads(await self.plugin.tool_photo(event, "portrait", request_summary="用户确认了条件", confirmed_request_id=request_id))
                self.assertFalse(result["ok"])
        self.assertEqual(self.store.session(key)["pending"]["request_id"], pending["request_id"])
        self.assertFalse(self.store.recent_jobs())

    async def test_request_id_without_semantic_basis_is_not_authorization(self):
        event = Event("收到啦")
        _key, pending = await self.seed_pending(event)
        result = json.loads(await self.plugin.tool_photo(event, "portrait", confirmed_request_id=pending["request_id"]))
        self.assertFalse(result["ok"])
        self.assertFalse(self.image_provider.calls)

    async def test_expired_conditions_cannot_be_confirmed_semantically(self):
        event = Event("你的约定我全部接受")
        key, pending = await self.seed_pending(event)
        session = self.store.session(key)
        session["pending"]["expires_at"] = time.time() - 1
        self.store.save_session(key, session)
        await self.request_hook(event)
        result = json.loads(await self.plugin.tool_photo(event, "portrait", request_summary="确认之前的条件", confirmed_request_id=pending["request_id"]))
        self.assertFalse(result["ok"])
        self.assertFalse(self.image_provider.calls)

    async def test_semantic_id_cannot_cross_conversations_or_personas(self):
        event = Event("你的约定我全部接受")
        _key, pending = await self.seed_pending(event)
        self.context.conversation_manager.cid = "other-conversation"
        result = json.loads(await self.plugin.tool_photo(event, "portrait", request_summary="确认条件", confirmed_request_id=pending["request_id"]))
        self.assertFalse(result["ok"])
        self.context.conversation_manager.cid = "conversation-1"
        other = copy.deepcopy(self.store.persona())
        other.update(id="other-role", name="另一个角色")
        self.store.upsert_persona(other)
        self.store.settings["current_persona"] = other["id"]
        result = json.loads(await self.plugin.tool_photo(event, "portrait", request_summary="确认条件", confirmed_request_id=pending["request_id"]))
        self.assertFalse(result["ok"])
        self.assertFalse(self.image_provider.calls)

    async def test_tool_rechecks_id_after_conditions_replaced_during_model_wait(self):
        event = Event("你的约定我全部接受")
        _key, pending = await self.seed_pending(event)
        await self.request_hook(event)
        env = await self.plugin.dialogue.environment(event.unified_msg_origin)
        current = self.plugin._save_conditions(env, "看一下你的自拍", "改成蓝裙远景，确认吗？", {"outfit": "blue dress", "camera": "wide shot"})
        result = json.loads(await self.plugin.tool_photo(event, "portrait", request_summary="确认旧条件", confirmed_request_id=pending["request_id"]))
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.session(env["key"])["pending"]["request_id"], current["request_id"])
        self.assertFalse(self.image_provider.calls)

    async def test_conditions_change_needs_a_new_confirmation(self):
        event = Event("约定我接受，但换上泳装")
        key, pending = await self.seed_pending(event)
        await self.request_hook(event)
        changed = json.loads(await self.plugin.tool_conditions(event, "泳装也只能远景，好吗？", outfit="swimsuit", request_summary="用户更换服装，需要重新询问"))
        self.assertTrue(changed["ok"])
        self.assertNotEqual(changed["request_id"], pending["request_id"])
        self.assertEqual(self.store.session(key)["pending"]["requirements"]["avoid"], "close-up")
        result = json.loads(await self.plugin.tool_photo(event, "swimsuit", request_summary="用户修改了条件，尚在询问", confirmed_request_id=changed["request_id"]))
        self.assertFalse(result["ok"])
        self.assertFalse(self.image_provider.calls)

    async def test_semantic_state_confirmation_only_updates_agreed_state(self):
        event = Event("你的约定我全部接受")
        key, pending = await self.seed_pending(event, kind="state")
        await self.request_hook(event)
        photo = json.loads(await self.plugin.tool_photo(event, "portrait", request_summary="用户确认换装条件", confirmed_request_id=pending["request_id"]))
        self.assertFalse(photo["ok"])
        state = json.loads(await self.plugin.tool_state(event, outfit="swimsuit", request_summary="用户确认换装原条件", confirmed_request_id=pending["request_id"]))
        self.assertTrue(state["ok"])
        self.assertFalse(state["photo_generated"])
        self.assertEqual(self.store.session(key)["state"]["outfit"], "white dress")
        self.assertFalse(self.image_provider.calls)

    async def test_semantic_state_request_without_keywords_does_not_take_photo(self):
        event = Event("还是刚才那件更适合你")
        self.assertFalse(state_request(event.message_str))
        result = json.loads(await self.plugin.tool_state(event, outfit="white dress", request_summary="用户要恢复上文约定的白裙，我同意换回"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["outfit"], "white dress")
        self.assertFalse(self.store.recent_jobs())

    async def test_withdrawal_and_only_quoted_text_veto_semantic_execution(self):
        for text in ("别拍了", "我不想看了", "好，不要拍了", "不要给我发自拍", "“拍给我看”", "```发吧```", ""):
            with self.subTest(text=text):
                event = Event(text)
                result = json.loads(await self.plugin.tool_photo(event, "portrait", request_summary="误判为用户要图片"))
                self.assertFalse(result["ok"], result)
        self.assertFalse(self.store.recent_jobs())
        self.assertFalse(self.image_provider.calls)

    async def test_negative_camera_constraint_is_not_request_withdrawal(self):
        event = Event("不要拍近景，我要看远一点的")
        result = json.loads(await self.plugin.tool_photo(event, "wide shot", request_summary="用户请求远景图片，并要求避免近景"))
        self.assertTrue(result["ok"], result)
        await self.settle_jobs()
        self.assertEqual(self.store.job(result["job_id"])["status"], "sent")

    async def test_unaddressed_group_or_disabled_integration_veto_semantics(self):
        event = self.group_event("让我见识一下今天这身打扮")
        event.is_at_or_wake_command = False
        result = json.loads(await self.plugin.tool_photo(event, "portrait", request_summary="用户要图片"))
        self.assertFalse(result["ok"])
        self.store.settings["integration"]["enabled"] = False
        result = json.loads(await self.plugin.tool_photo(Event("让我见识一下今天这身打扮"), "portrait", request_summary="用户要图片"))
        self.assertFalse(result["ok"])
        self.assertFalse(self.image_provider.calls)

    async def test_failed_semantic_tool_does_not_clear_pending_in_response_hook(self):
        event = Event("你的约定我全部接受")
        key, pending = await self.seed_pending(event)
        await self.request_hook(event)
        result = json.loads(await self.plugin.tool_photo(event, "portrait", request_summary="确认条件", confirmed_request_id="old-id"))
        self.assertFalse(result["ok"])
        await self.plugin.remember_conditions(event, types.SimpleNamespace(completion_text="这次工具没有成功。"))
        self.assertEqual(self.store.session(key)["pending"]["request_id"], pending["request_id"])

    async def test_words_of_agreement_without_a_tool_cannot_create_a_job(self):
        event = Event("让我见识一下今天这身打扮")
        await self.request_hook(event)
        await self.plugin.remember_conditions(event, types.SimpleNamespace(completion_text="好呀，我愿意给你看。"))
        self.assertFalse(self.store.recent_jobs())
        self.assertFalse(self.image_provider.calls)


if __name__ == "__main__":
    unittest.main()
