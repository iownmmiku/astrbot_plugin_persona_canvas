"""Offline reply decoration with real storage/generation and fake platform IO."""
import asyncio
import copy
import json
import types
import unittest
from unittest.mock import patch

import test_core as fixtures
from test_core import Event
from canvas_test.reply_images import ReplyImages
from astrbot.api.message_components import Image


class Plain:
    def __init__(self, text):
        self.text = text


class Result:
    def __init__(self, text="今天的海风很舒服，一起坐一会吧。", *, llm=True, streaming=False):
        self.chain = [Plain(text)]
        self.llm = llm
        self.async_stream = None
        self.result_content_type = types.SimpleNamespace(name="STREAMING_FINISH" if streaming else "LLM_RESULT")

    def is_llm_result(self):
        return self.llm

    def get_plain_text(self):
        return " ".join(item.text for item in self.chain if isinstance(item, Plain))


class ReplyImageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    def event(self, text="今天海边的天气真好", *, identifier="m-1", umo="qq:FriendMessage:123", sender="123", private=True, reply=None):
        event = Event(text, umo)
        event.message_obj = types.SimpleNamespace(message_id=identifier)
        event.is_private_chat = lambda: private
        event.get_sender_id = lambda: sender
        event.result = Result() if reply is None else reply
        event.get_result = lambda: event.result
        event.is_stopped = lambda: False
        return event

    def enable(self, event, probability=100):
        self.store.settings["reply_images"] = {"enabled": True, "default_probability": 20, "min_interval_sec": 0, "provider": ""}
        self.store.targets["items"] = [{"umo": event.unified_msg_origin, "scope": "private" if event.is_private_chat() else "group", "reply_image_enabled": True, "reply_image_probability": probability}]
        self.context.llm.next = {"decision": "photo", "prompt": "坐在海边，微笑，柔和日光", "state_patch": {"pose": "sitting by the sea"}}
        self.plugin.reply_images = ReplyImages(self.plugin)

    async def wait_for_generation(self):
        for _ in range(200):
            if self.image_provider.calls:
                return
            await asyncio.sleep(.001)
        self.fail("generation did not start")

    async def test_disabled_by_default_has_no_model_or_image_io(self):
        event = self.event()
        await self.plugin.reply_images.decorate(event)
        self.assertFalse(self.context.llm.calls)
        self.assertFalse(self.image_provider.calls)
        self.assertEqual(len(event.result.chain), 1)

    async def test_chat_opt_in_is_required_separately(self):
        event = self.event()
        self.enable(event)
        self.store.targets["items"][0]["reply_image_enabled"] = False
        await self.plugin.reply_images.decorate(event)
        self.assertFalse(self.context.llm.calls)
        other = self.event(umo="qq:FriendMessage:456")
        await self.plugin.reply_images.decorate(other)
        self.assertFalse(self.context.llm.calls)

    async def test_complete_reply_keeps_text_and_appends_one_saved_image(self):
        event = self.event()
        self.enable(event)
        original = event.result.chain[0]
        env = await self.plugin._env(event)
        before = copy.deepcopy(self.store.read_session(env["key"]))
        await self.plugin.reply_images.decorate(event)
        self.assertIs(event.result.chain[0], original)
        self.assertEqual(len(event.result.chain), 2)
        self.assertFalse(event.sent)
        self.assertFalse(self.context.sent)
        job = self.store.recent_jobs()[0]
        self.assertEqual(job["source"], "reply_image")
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["delivery_state"], "attached")
        self.assertFalse(job["state_committed"])
        self.assertEqual(self.store.read_session(env["key"]), before)
        self.assertTrue(self.store.asset(job["asset"]).is_file())

    async def test_model_may_decline_without_error_text_or_generation(self):
        event = self.event()
        self.enable(event)
        self.context.llm.next = {"decision": "skip", "prompt": ""}
        await self.plugin.reply_images.decorate(event)
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertFalse(self.image_provider.calls)
        self.assertEqual(len(event.result.chain), 1)

    async def test_probability_miss_deduplicates_same_message_replay(self):
        event = self.event()
        self.enable(event, probability=20)
        with patch("canvas_test.reply_images.random.random", return_value=.8) as draw:
            await self.plugin.reply_images.decorate(event)
            await self.plugin.reply_images.decorate(self.event())
        self.assertEqual(draw.call_count, 1)
        self.assertFalse(self.context.llm.calls)

    async def test_zero_probability_never_calls_decision(self):
        event = self.event()
        self.enable(event, probability=0)
        await self.plugin.reply_images.decorate(event)
        self.assertFalse(self.context.llm.calls)

    async def test_per_chat_probability_overrides_default(self):
        event = self.event()
        self.enable(event, probability=80)
        self.store.settings["reply_images"]["default_probability"] = 10
        with patch("canvas_test.reply_images.random.random", return_value=.5):
            await self.plugin.reply_images.decorate(event)
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_commands_explicit_requests_withdrawals_and_refusals_are_excluded(self):
        for text, reply in [("/帮助", None), ("看看自拍", None), ("不要发图了", None), ("换上白裙", None), ("“拍一张自拍”", None), ("今天天气真好", Result("我现在不想拍照。"))]:
            with self.subTest(text=text):
                event = self.event(text, reply=reply)
                self.enable(event)
                await self.plugin.reply_images.decorate(event)
                self.assertEqual(len(event.result.chain), 1)
        self.assertFalse(self.context.llm.calls)

    async def test_existing_image_media_non_llm_and_streams_are_excluded(self):
        image_reply = Result()
        image_reply.chain.append(Image())
        audio_reply = Result()
        audio_reply.chain.append(type("Record", (), {})())
        for result in [image_reply, audio_reply, Result(llm=False), Result(streaming=True), Result("")]:
            event = self.event(reply=result)
            self.enable(event)
            await self.plugin.reply_images.decorate(event)
        self.assertFalse(self.context.llm.calls)

    async def test_tools_conditions_and_handled_turns_do_not_get_random_photo(self):
        for key in ("native_tool_seen", "photo_attempted", "conditions_recorded", "handled", "request_cancelled"):
            event = self.event(identifier=key)
            self.enable(event)
            event.set_extra("persona_canvas:" + key, True)
            await self.plugin.reply_images.decorate(event)
        self.assertFalse(self.context.llm.calls)

    async def test_pending_conditions_remain_unchanged(self):
        event = self.event()
        self.enable(event)
        env = await self.plugin._env(event)
        self.plugin._save_conditions(env, "泳装自拍", "先穿好外套，可以吗？", {"outfit": "外套"})
        before = self.store.read_session(env["key"])
        await self.plugin.reply_images.decorate(event)
        self.assertFalse(self.context.llm.calls)
        self.assertEqual(self.store.read_session(env["key"]), before)

    async def test_current_chat_model_ignores_separate_decision_model_override(self):
        event = self.event()
        self.enable(event)
        alternate = fixtures.LLM()
        self.context.get_provider_by_id = lambda identifier: alternate if identifier == "alternate" else None
        self.store.settings["llm"].update(provider_id="alternate", model="override-model")
        await self.plugin.reply_images.decorate(event)
        self.assertFalse(alternate.calls)
        self.assertEqual(len(self.context.llm.calls), 1)
        call = self.context.llm.calls[0]
        self.assertNotIn("model", call)
        self.assertIn(event.message_str, call["prompt"])
        self.assertIn(event.result.get_plain_text(), call["prompt"])
        self.assertIn("害羞", call["system_prompt"])

    async def test_provider_failure_keeps_original_reply(self):
        event = self.event()
        self.enable(event)
        self.image_provider.error = "offline workflow unavailable"
        await self.plugin.reply_images.decorate(event)
        self.assertEqual(len(event.result.chain), 1)
        self.assertFalse(event.sent)
        self.assertEqual(self.store.recent_jobs()[0]["status"], "failed")
        self.assertIn("offline", self.store.recent_jobs()[0]["error"])

    async def test_invalid_model_decision_keeps_original_reply(self):
        event = self.event()
        self.enable(event)
        self.context.llm.next = {"decision": "ask", "reply": "可以吗？"}
        await self.plugin.reply_images.decorate(event)
        self.assertEqual(len(event.result.chain), 1)
        self.assertFalse(self.image_provider.calls)

    async def test_global_integration_switch_disables_reply_images(self):
        event = self.event()
        self.enable(event)
        self.store.settings["integration"]["enabled"] = False
        await self.plugin.reply_images.decorate(event)
        self.assertFalse(self.context.llm.calls)

    async def test_cooldown_is_per_chat_and_survives_service_recreation(self):
        event = self.event()
        self.enable(event)
        self.store.settings["reply_images"]["min_interval_sec"] = 300
        await self.plugin.reply_images.decorate(event)
        self.plugin.reply_images = ReplyImages(self.plugin)
        await self.plugin.reply_images.decorate(self.event(identifier="m-2"))
        self.assertEqual(len(self.image_provider.calls), 1)
        other = self.event(identifier="m-3", umo="qq:FriendMessage:456")
        self.store.targets["items"].append({"umo": other.unified_msg_origin, "reply_image_enabled": True, "reply_image_probability": 100})
        await self.plugin.reply_images.decorate(other)
        self.assertEqual(len(self.image_provider.calls), 2)

    async def test_group_chat_requires_no_at_and_quota_uses_actual_sender(self):
        event = self.event(umo="qq:GroupMessage:999", sender="alice", private=False)
        event.is_at_or_wake_command = False
        self.enable(event)
        self.store.settings["moderation"]["daily_limit"] = 1
        await self.plugin.reply_images.decorate(event)
        other = self.event(identifier="m-2", umo=event.unified_msg_origin, sender="bob", private=False)
        await self.plugin.reply_images.decorate(other)
        again = self.event(identifier="m-3", umo=event.unified_msg_origin, sender="alice", private=False)
        await self.plugin.reply_images.decorate(again)
        self.assertEqual(len(self.image_provider.calls), 2)
        self.assertEqual(len(event.result.chain), 2)
        self.assertEqual(len(other.result.chain), 2)
        self.assertEqual(len(again.result.chain), 1)

    async def test_same_event_observed_twice_keeps_one_turn_token(self):
        event = self.event()
        self.enable(event)
        token = self.plugin.reply_images.observe(event)
        self.assertEqual(self.plugin.reply_images.observe(event), token)
        await self.plugin.reply_images.decorate(event)
        await self.plugin.reply_images.decorate(event)
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_new_inbound_message_discards_inflight_photo(self):
        event = self.event()
        self.enable(event)
        self.image_provider.pause = asyncio.Event()
        task = asyncio.create_task(self.plugin.reply_images.decorate(event))
        await self.wait_for_generation()
        self.plugin.reply_images.observe(self.event("先等等", identifier="new-turn"))
        self.image_provider.pause.set()
        await task
        self.assertEqual(len(event.result.chain), 1)
        self.assertEqual(self.store.recent_jobs()[0]["delivery_state"], "discarded")

    async def test_disabling_chat_while_generating_does_not_append_old_image(self):
        event = self.event()
        self.enable(event)
        self.image_provider.pause = asyncio.Event()
        task = asyncio.create_task(self.plugin.reply_images.decorate(event))
        await self.wait_for_generation()
        self.store.targets["items"][0]["reply_image_enabled"] = False
        self.image_provider.pause.set()
        await task
        self.assertEqual(len(event.result.chain), 1)
        self.assertEqual(self.store.recent_jobs()[0]["delivery_state"], "discarded")

    async def test_persona_change_while_generating_does_not_restore_old_role(self):
        event = self.event()
        self.enable(event)
        self.image_provider.pause = asyncio.Event()
        task = asyncio.create_task(self.plugin.reply_images.decorate(event))
        await self.wait_for_generation()
        second = {**copy.deepcopy(self.store.personas["items"][0]), "id": "second", "name": "新角色"}
        self.store.personas["items"].append(second)
        self.store.targets["items"][0]["persona_id"] = "second"
        self.image_provider.pause.set()
        await task
        self.assertEqual(len(event.result.chain), 1)
        env = await self.plugin.dialogue.environment(event.unified_msg_origin)
        self.assertEqual(env["persona"]["id"], "second")
        self.assertEqual(self.store.read_session(env["key"])["persona_id"], "second")

    async def test_new_pending_conditions_during_generation_are_preserved(self):
        event = self.event()
        self.enable(event)
        self.image_provider.pause = asyncio.Event()
        task = asyncio.create_task(self.plugin.reply_images.decorate(event))
        await self.wait_for_generation()
        env = await self.plugin._env(event)
        pending = self.plugin._save_conditions(env, "自拍", "只拍半身，可以吗？", {"camera": "half body"})
        self.image_provider.pause.set()
        await task
        self.assertEqual(len(event.result.chain), 1)
        self.assertEqual(self.store.read_session(env["key"])["pending"]["request_id"], pending["request_id"])

    async def test_concurrent_duplicate_decorators_generate_once(self):
        event = self.event()
        self.enable(event)
        self.image_provider.pause = asyncio.Event()
        first = asyncio.create_task(self.plugin.reply_images.decorate(event))
        await self.wait_for_generation()
        await self.plugin.reply_images.decorate(self.event())
        self.image_provider.pause.set()
        await first
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(len(self.context.llm.calls), 1)

    async def test_scene_illustration_does_not_include_character_identity(self):
        event = self.event()
        self.enable(event)
        self.store.personas["items"][0]["positive_prompt"] = "unique-character-identity"
        self.context.llm.next = {"decision": "scene", "prompt": "海边的夕阳，海鸥与灯塔"}
        await self.plugin.reply_images.decorate(event)
        self.assertEqual(self.store.recent_jobs()[0]["mode"], "scene")
        self.assertNotIn("unique-character-identity", self.image_provider.calls[0]["positive"])

    async def test_user_cancelling_job_keeps_original_reply_pipeline_running(self):
        event = self.event()
        self.enable(event)
        self.image_provider.pause = asyncio.Event()
        task = asyncio.create_task(self.plugin.reply_images.decorate(event))
        await self.wait_for_generation()
        job = self.store.recent_jobs()[0]
        await self.plugin.web_cancel_job({"id": job["id"]})
        await task
        self.assertFalse(task.cancelled())
        self.assertEqual(task.cancelling(), 0)
        self.assertEqual(len(event.result.chain), 1)
        current = self.store.job(job["id"])
        self.assertEqual(current["status"], "cancelled")
        self.assertEqual(current["delivery_state"], "discarded")
        allowed, _, token = self.plugin.moderation.reserve("qq:123", "test")
        self.assertTrue(allowed)
        self.plugin.moderation.finish("qq:123", success=False, token=token)

    async def test_plugin_shutdown_cancellation_propagates(self):
        event = self.event()
        self.enable(event)
        self.image_provider.pause = asyncio.Event()
        task = asyncio.create_task(self.plugin.reply_images.decorate(event))
        await self.wait_for_generation()
        self.plugin._closing = True
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(event.result.chain), 1)
        self.assertEqual(self.store.recent_jobs()[0]["status"], "cancelled")

    async def test_pipeline_cancellation_without_user_withdrawal_propagates(self):
        event = self.event()
        self.enable(event)
        self.image_provider.pause = asyncio.Event()
        task = asyncio.create_task(self.plugin.reply_images.decorate(event))
        await self.wait_for_generation()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.plugin.reply_images._inflight)

    async def test_provider_initialisation_failure_has_visible_failed_job(self):
        event = self.event()
        self.enable(event)
        def unavailable(_name=None):
            raise ValueError("工作流尚未配置")
        self.plugin._provider = unavailable
        await self.plugin.reply_images.decorate(event)
        self.assertEqual(len(event.result.chain), 1)
        job = self.store.recent_jobs()[0]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["failure_stage"], "provider")
        self.assertIn("工作流", job["error"])

    async def test_switching_chat_off_during_role_decision_prevents_generation(self):
        event = self.event()
        self.enable(event)
        started, release = asyncio.Event(), asyncio.Event()
        original = self.context.llm.text_chat
        async def delayed(**kwargs):
            started.set()
            await release.wait()
            return await original(**kwargs)
        self.context.llm.text_chat = delayed
        task = asyncio.create_task(self.plugin.reply_images.decorate(event))
        await asyncio.wait_for(started.wait(), 2)
        self.store.targets["items"][0]["reply_image_enabled"] = False
        release.set()
        await task
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.store.recent_jobs())
        self.assertEqual(len(event.result.chain), 1)

    async def test_model_tool_response_never_starts_image_generation(self):
        event = self.event()
        self.enable(event)
        async def tool_response(**kwargs):
            return types.SimpleNamespace(completion_text=json.dumps(self.context.llm.next), tools_call_name=["unwanted_tool"])
        self.context.llm.text_chat = tool_response
        await self.plugin.reply_images.decorate(event)
        self.assertFalse(self.image_provider.calls)
        self.assertEqual(len(event.result.chain), 1)

    async def test_real_terminate_during_role_decision_cancels_before_database_close(self):
        event = self.event()
        self.enable(event)
        started = asyncio.Event()
        async def delayed(**kwargs):
            started.set()
            await asyncio.Event().wait()
        self.context.llm.text_chat = delayed
        task = asyncio.create_task(self.plugin.reply_images.decorate(event))
        await asyncio.wait_for(started.wait(), 2)
        self.assertIn(task, self.plugin._requests)
        original = event.result.chain[0]
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertEqual(event.result.chain, [original])
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.plugin.reply_images._inflight)

    async def test_real_terminate_during_provider_selection_cancels_pipeline(self):
        event = self.event()
        self.enable(event)
        started = asyncio.Event()
        async def delayed_provider(umo="", *, current_only=False):
            started.set()
            await asyncio.Event().wait()
        self.plugin.dialogue.provider = delayed_provider
        task = asyncio.create_task(self.plugin.reply_images.decorate(event))
        await asyncio.wait_for(started.wait(), 2)
        self.assertIn(task, self.plugin._requests)
        await self.plugin.terminate()
        self.assertTrue(task.cancelled())
        self.assertEqual(len(event.result.chain), 1)
        self.assertFalse(self.context.llm.calls)
        self.assertFalse(self.image_provider.calls)


if __name__ == "__main__":
    unittest.main()
