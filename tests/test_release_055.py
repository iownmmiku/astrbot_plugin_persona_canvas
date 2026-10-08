"""Offline acceptance cases for semantic controls and a single photo request."""
import asyncio
import base64
import copy
import io
import json
import types
import unittest
from unittest.mock import AsyncMock

from PIL import Image

import test_core as fixtures
import test_tool_failures as group_fixtures
from test_core import Event, image_bytes
from canvas_test.providers.base import ProviderError, provider_from_config


class Release055Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    consume = fixtures.RuntimeTests.consume
    request_hook = group_fixtures.GroupConfirmationTests.request_hook
    seed_pending = group_fixtures.GroupConfirmationTests.seed_pending

    async def settle_jobs(self):
        while self.plugin._tasks:
            await asyncio.gather(*list(self.plugin._tasks), return_exceptions=True)

    async def wait_for_provider(self):
        for _ in range(100):
            if self.image_provider.calls:
                return
            await asyncio.sleep(.01)
        self.fail("Queued job never reached the offline provider")

    async def confirm(self, event, request_id, *, updates=None):
        result = json.loads(await self.plugin.tool_control(event, "confirm", "用户接受原来的拍摄约定，没有修改条件。", request_id=request_id, updates=updates))
        self.assertTrue(result["ok"], result)
        return result

    def event_with_image(self, text):
        output = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(output, "PNG")
        data = output.getvalue()
        component = fixtures.components_module.Image()
        component.convert_to_base64 = AsyncMock(return_value=base64.b64encode(data).decode("ascii"))
        event = Event(text)
        event.get_messages = lambda: [component]
        return event, component, data

    async def test_agreed_avoid_terms_only_enter_negative_prompt(self):
        event = Event("约法三章都听懂了，就照你的安排")
        _key, pending = await self.seed_pending(event)
        await self.request_hook(event)
        await self.confirm(event, pending["request_id"])
        result = json.loads(await self.plugin.tool_photo(event, "portrait, white dress, wide shot"))
        self.assertTrue(result["ok"], result)
        await self.settle_jobs()
        call = self.image_provider.calls[0]
        self.assertIn("wide shot", call["positive"])
        self.assertNotIn("close-up", call["positive"])
        self.assertIn("close-up", call["negative"])
        self.assertEqual(self.store.job(result["job_id"])["requirements"]["avoid"], "close-up")

    async def test_two_events_confirming_same_pending_create_one_provider_request(self):
        self.image_provider.pause = asyncio.Event()
        first = Event("我同意你刚才全部约定，拍吧")
        _key, pending = await self.seed_pending(first)
        await self.request_hook(first)
        await self.confirm(first, pending["request_id"])
        queued = json.loads(await self.plugin.tool_photo(first, "portrait"))
        self.assertTrue(queued["ok"], queued)
        await self.wait_for_provider()

        second = Event("听明白了，就按原条件来")
        await self.request_hook(second)
        await self.confirm(second, pending["request_id"])
        repeated = json.loads(await self.plugin.tool_photo(second, "portrait"))
        if repeated.get("ok"):
            self.assertEqual(repeated.get("job_id"), queued["job_id"], repeated)
        await asyncio.sleep(0)
        self.assertEqual(len(self.store.recent_jobs()), 1)
        self.assertEqual(len(self.image_provider.calls), 1)
        self.image_provider.pause.set()
        await self.settle_jobs()
        self.assertEqual(self.store.job(queued["job_id"])["status"], "sent")

    async def test_failed_confirmation_requires_explicit_retry_before_second_call(self):
        self.image_provider.error = "offline upstream failure"
        first = Event("我确认原来的约定，拍吧")
        _key, pending = await self.seed_pending(first)
        await self.request_hook(first)
        await self.confirm(first, pending["request_id"])
        queued = json.loads(await self.plugin.tool_photo(first, "portrait"))
        self.assertTrue(queued["ok"], queued)
        await self.settle_jobs()
        self.assertEqual(self.store.job(queued["job_id"])["status"], "failed")

        second = Event("我也同意原来的约定")
        await self.request_hook(second)
        await self.confirm(second, pending["request_id"])
        repeated = json.loads(await self.plugin.tool_photo(second, "portrait"))
        if repeated.get("ok"):
            self.assertEqual(repeated.get("job_id"), queued["job_id"], repeated)
        await self.settle_jobs()
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(len(self.store.recent_jobs()), 1)

        self.image_provider.error = None
        retry = Event("刚刚那张失败了，请再试一次")
        await self.request_hook(retry)
        retried = json.loads(await self.plugin.tool_control(retry, "retry", "用户明确要求重试刚才失败的照片，保持原来的条件。", job_id=queued["job_id"]))
        self.assertTrue(retried["ok"], retried)
        await self.settle_jobs()
        self.assertNotEqual(retried["job_id"], queued["job_id"])
        self.assertEqual(len(self.image_provider.calls), 2)
        self.assertEqual(self.store.job(retried["job_id"])["status"], "sent")

    async def test_completed_old_job_does_not_restore_a_switched_persona(self):
        self.image_provider.pause = asyncio.Event()
        event = Event("拍一张你的自拍")
        old = await self.plugin.dialogue.environment(event.unified_msg_origin)
        queued = json.loads(await self.plugin.tool_photo(event, "portrait", outfit="white dress"))
        self.assertTrue(queued["ok"], queued)
        await self.wait_for_provider()
        self.store.upsert_persona({"id": "new-persona", "name": "另一个角色", "state": {"outfit": "black dress"}})
        self.store.settings["current_persona"] = "new-persona"
        switched = await self.plugin.dialogue.environment(event.unified_msg_origin)
        self.assertEqual(switched["persona"]["id"], "new-persona")
        self.image_provider.pause.set()
        await self.settle_jobs()
        session = self.store.session(old["key"])
        self.assertEqual(session["persona_id"], "new-persona")
        self.assertEqual(session["state"]["outfit"], "black dress")
        self.assertFalse(session.get("last_image"))
        self.assertFalse(self.store.job(queued["job_id"])["state_committed"])

    async def test_normal_native_explanation_does_not_discard_pending_conditions(self):
        event = Event("我今天心情有点复杂，先聊两句")
        key, pending = await self.seed_pending(event)
        await self.request_hook(event)
        await self.plugin.remember_conditions(event, types.SimpleNamespace(completion_text="那就先聊聊今天的心情吧。"))
        self.assertEqual(self.store.session(key)["pending"]["request_id"], pending["request_id"])
        self.assertFalse(self.image_provider.calls)

    async def test_semantic_control_accepts_repeating_the_agreed_white_dress(self):
        event = Event("我全部同意，按约定穿上白裙就好")
        _key, pending = await self.seed_pending(event)
        await self.request_hook(event)
        await self.confirm(event, pending["request_id"], updates={"outfit": "white dress"})
        queued = json.loads(await self.plugin.tool_photo(event, "portrait, white dress"))
        self.assertTrue(queued["ok"], queued)
        await self.settle_jobs()
        self.assertEqual(self.store.job(queued["job_id"])["state_patch"]["outfit"], "white dress")
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_semantic_cancel_stops_running_job_without_keyword_command(self):
        self.image_provider.pause = asyncio.Event()
        event = Event("给我拍一张你的自拍")
        queued = json.loads(await self.plugin.tool_photo(event, "portrait"))
        self.assertTrue(queued["ok"], queued)
        await self.wait_for_provider()
        cancelled = json.loads(await self.plugin.tool_control(Event("我不想看了"), "cancel", "用户撤回正在生成的照片，不再希望收到。", job_id=queued["job_id"]))
        self.assertTrue(cancelled["ok"], cancelled)
        await self.settle_jobs()
        job = self.store.job(queued["job_id"])
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(job["cancel_requested"])
        self.assertFalse(event.sent)
        self.assertFalse(job.get("asset"))

    async def test_normal_chat_image_is_not_converted_or_saved_by_injection(self):
        event, component, _data = self.event_with_image("看这个风景，我刚才在路上看到的")
        before = set(self.store.assets.iterdir())
        await self.request_hook(event)
        await self.plugin.remember_conditions(event, types.SimpleNamespace(completion_text="这个景色很好看。"))
        component.convert_to_base64.assert_not_awaited()
        self.assertEqual(set(self.store.assets.iterdir()), before)
        self.assertFalse(self.store.recent_jobs())

    async def test_persona_selfie_uses_role_reference_without_reading_user_attachment(self):
        role_asset = self.store.save_asset(image_bytes(), "png").name
        persona = copy.deepcopy(self.store.persona())
        persona.update(reference_asset=role_asset, reference_assets=[role_asset], reference_enabled=True)
        self.store.upsert_persona(persona)
        event, component, _data = self.event_with_image("给我拍一张你的自拍")
        await self.request_hook(event)
        queued = json.loads(await self.plugin.tool_photo(event, "portrait"))
        self.assertTrue(queued["ok"], queued)
        await self.settle_jobs()
        component.convert_to_base64.assert_not_awaited()
        self.assertEqual(self.image_provider.calls[0]["reference"], image_bytes())
        self.assertEqual(self.store.job(queued["job_id"])["reference_assets"], [role_asset])

    async def test_edit_mode_lazily_reads_message_attachment(self):
        event, component, data = self.event_with_image("把这张图的背景换成夜景")
        await self.request_hook(event)
        component.convert_to_base64.assert_not_awaited()
        queued = json.loads(await self.plugin.tool_photo(event, "change background to night", mode="edit"))
        self.assertTrue(queued["ok"], queued)
        await self.settle_jobs()
        component.convert_to_base64.assert_awaited_once()
        self.assertEqual(self.image_provider.calls[0]["reference"], data)
        self.assertEqual(self.store.job(queued["job_id"])["status"], "sent")

    async def test_explicit_message_reference_can_be_used_for_persona_photo(self):
        event, component, data = self.event_with_image("参考我这张图，拍一张你的自拍")
        await self.request_hook(event)
        queued = json.loads(await self.plugin.tool_photo(event, "portrait", use_message_image=True))
        self.assertTrue(queued["ok"], queued)
        await self.settle_jobs()
        component.convert_to_base64.assert_awaited_once()
        self.assertEqual(self.image_provider.calls[0]["reference"], data)

    async def test_revise_requires_new_confirmation_and_preserves_other_conditions(self):
        event = Event("镜头近一点就好")
        key, pending = await self.seed_pending(event)
        await self.request_hook(event)
        revised = json.loads(await self.plugin.tool_control(event, "revise", "用户要求把远景改成近景，需要确认新条件。", request_id=pending["request_id"], reply="白裙近景，不要腹肌，可以吗？", updates={"camera": "close-up", "avoid": "abs"}))
        self.assertTrue(revised["ok"], revised)
        self.assertNotEqual(revised["request_id"], pending["request_id"])
        self.assertFalse(json.loads(await self.plugin.tool_photo(event, "portrait, close-up"))["ok"])
        self.assertFalse(self.image_provider.calls)
        changed = self.store.session(key)["pending"]
        self.assertEqual(changed["requirements"]["outfit"], "white dress")
        self.assertEqual(changed["requirements"]["camera"], "close-up")
        next_event = Event("按你新说的来")
        await self.request_hook(next_event)
        await self.confirm(next_event, revised["request_id"])
        queued = json.loads(await self.plugin.tool_photo(next_event, "portrait, close-up"))
        self.assertTrue(queued["ok"], queued)
        await self.settle_jobs()
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_confirm_cannot_hide_a_structured_condition_change(self):
        event = Event("听懂了")
        key, pending = await self.seed_pending(event)
        rejected = json.loads(await self.plugin.tool_control(event, "confirm", "用户确认条件", request_id=pending["request_id"], updates={"outfit": "swimsuit"}))
        self.assertFalse(rejected["ok"], rejected)
        self.assertEqual(self.store.session(key)["pending"]["requirements"]["outfit"], "white dress")
        self.assertFalse(self.image_provider.calls)

    async def test_failed_history_keeps_actual_provider_request_summary(self):
        item = provider_from_config("drawing", {"kind": "openai", "endpoint": "https://provider.example", "model": "anime-model", "api_key": "private-test-key", "negative_mode": "field"})
        item._post = AsyncMock(side_effect=ProviderError("offline provider failure"))
        self.plugin._provider = lambda name=None: item
        queued = json.loads(await self.plugin.tool_photo(Event("拍一张你的自拍"), "portrait"))
        self.assertTrue(queued["ok"], queued)
        await self.settle_jobs()
        job = self.store.job(queued["job_id"])
        history = next(row for row in self.store.recent_history() if row.get("job_id") == queued["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertFalse(history["ok"])
        summary = history["request_summary"]
        self.assertEqual(summary["prompt"], item._post.call_args.args[1]["prompt"])
        self.assertEqual(summary["negative_prompt"], item._post.call_args.args[1]["negative_prompt"])
        self.assertEqual(summary["negative_mode"], "field")
        self.assertEqual(summary, job["request_summary"])
        self.assertNotIn("private-test-key", json.dumps(summary))

    async def test_cached_cancellation_does_not_restore_previous_persona(self):
        event = Event("别拍了")
        key, _pending = await self.seed_pending(event)
        await self.request_hook(event)
        other = copy.deepcopy(self.store.persona())
        other.update(id="other", name="另一个角色", state={**other["state"], "outfit": "black dress"})
        self.store.upsert_persona(other)
        self.store.settings["current_persona"] = "other"
        switched = await self.plugin.dialogue.environment(event.unified_msg_origin)
        self.plugin._save_conditions(switched, "新请求", "黑裙远景，可以吗？", {"outfit": "black dress"})
        before = self.store.read_session(key)
        await self.consume(self.plugin.cancel_photo(event))
        self.assertEqual(self.store.read_session(key), before)

    async def test_cancel_failed_unconditional_photo_revokes_retry_authorization(self):
        self.image_provider.error = "offline failure"
        event = Event("拍一张自拍")
        queued = json.loads(await self.plugin.tool_photo(event, "portrait"))
        await self.settle_jobs()
        self.assertEqual(self.store.job(queued["job_id"])["status"], "failed")
        cancellation = json.loads(await self.plugin.tool_control(Event("我不想看了"), "cancel", "用户撤回失败的图片请求", job_id=queued["job_id"]))
        self.assertTrue(cancellation["ok"], cancellation)
        with self.assertRaises(ValueError):
            await self.plugin.web_retry_job({"id": queued["job_id"]})
        self.assertEqual(len(self.image_provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
