"""Delivery identity, optional-work deadlines and generation queue fairness."""
import asyncio
import copy
import unittest
from unittest.mock import patch

import test_core as fixtures
import test_reply_images as replies
from canvas_test import reply_images


class DeliveryPolicyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    async def saved(self):
        env = await self.plugin._env(fixtures.Event("看看自拍"))
        return env, await self.plugin._generate_job(env, "portrait", source="chat", is_admin=True)

    async def test_conversation_switch_blocks_saved_image_without_schedule_plugin(self):
        env, job = await self.saved()
        self.context.conversation_manager.cid = "new-conversation"
        with self.assertRaisesRegex(fixtures.main.DeliveryRejectedError, "会话"):
            await self.plugin._send_job(job, umo=env["umo"])
        self.assertFalse(self.context.sent)
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.assertTrue(self.store.job(job["id"])["delivery_blocked"])

    async def test_changed_visual_conditions_block_old_saved_photo(self):
        env, job = await self.saved()
        self.plugin._save_conditions(env, "改穿外套", "外套可以吗", {"outfit": "coat"})
        with self.assertRaises(fixtures.main.DeliveryRejectedError):
            await self.plugin._send_job(job, umo=env["umo"])
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.read_session(env["key"])["pending"]["requirements"]["outfit"], "coat")

    async def test_visual_binding_change_is_checked_without_mutating_session(self):
        env, job = await self.saved()
        self.store.personas["items"].append({**copy.deepcopy(self.store.persona()), "id": "other"})
        self.store.targets["items"] = [{"umo": env["umo"], "persona_id": "other"}]
        before = self.store.read_session(env["key"])
        self.assertIn("视觉角色", await self.plugin._delivery_guard(job=job))
        self.assertEqual(self.store.read_session(env["key"]), before)

    async def test_settings_are_rechecked_after_identity_lookup_yields(self):
        env, job = await self.saved()
        original = self.plugin.dialogue.current_identity
        async def changed(umo):
            self.store.settings["integration"]["enabled"] = False
            return await original(umo)
        self.plugin.dialogue.current_identity = changed
        self.assertIn("停用", await self.plugin._delivery_guard(job=job))

    async def test_decision_cannot_update_old_conversation_after_model_wait(self):
        env = await self.plugin._env(fixtures.Event())
        self.context.conversation_manager.cid = "changed"
        before = self.store.read_session(env["key"])
        with self.assertRaisesRegex(ValueError, "会话"):
            await self.plugin._apply_decision(env, {"decision": "state", "state_patch": {"outfit": "coat"}}, "换上外套", explicit=True)
        self.assertEqual(self.store.read_session(env["key"]), before)

    async def test_explicit_requests_take_priority_over_waiting_automatic_images(self):
        self.store.settings["generation"]["max_concurrency"] = 1
        order = []
        async def worker(source):
            async with self.plugin._generation_slot(source):
                order.append(source)
                await asyncio.sleep(0)
        async with self.plugin._generation_slot():
            automatic = asyncio.create_task(worker("reply_image"))
            await asyncio.sleep(0)
            explicit = asyncio.create_task(worker("chat"))
            await asyncio.sleep(0)
        await asyncio.gather(automatic, explicit)
        self.assertEqual(order, ["chat", "reply_image"])
        self.assertFalse(self.plugin._generation_waiters)
        self.assertEqual(self.plugin._running, 0)

    async def test_cancelled_waiter_does_not_block_queue_or_take_a_slot(self):
        self.store.settings["generation"]["max_concurrency"] = 1
        async def wait():
            async with self.plugin._generation_slot("reply_image"):
                self.fail("cancelled waiter acquired a slot")
        async with self.plugin._generation_slot():
            task = asyncio.create_task(wait())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(self.plugin._generation_waiters)
        self.assertEqual(self.plugin._running, 0)


class ReplyDeadlineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    event = replies.ReplyImageTests.event
    enable = replies.ReplyImageTests.enable

    def short_deadline(self):
        number = reply_images._number
        return patch.object(reply_images, "_number", side_effect=lambda value, default, minimum, maximum: .03 if minimum == 5 and maximum == 600 else number(value, default, minimum, maximum))

    async def test_slow_decision_keeps_original_text_without_cancelling_caller(self):
        event = self.event()
        self.enable(event)
        async def slow(*_args):
            await asyncio.Event().wait()
        self.plugin.reply_images._decide = slow
        with self.short_deadline():
            await asyncio.wait_for(self.plugin.reply_images.decorate(event), 1)
        await asyncio.sleep(0)
        self.assertEqual(len(event.result.chain), 1)
        self.assertFalse(self.image_provider.calls)
        self.assertEqual(asyncio.current_task().cancelling(), 0)
        state = self.store.delivery(event.get_extra("persona_canvas:reply_image_delivery"))
        self.assertEqual(state["skip_reason"], "reply_image_timeout")

    async def test_waiting_for_generation_slot_is_part_of_deadline(self):
        event = self.event()
        self.enable(event)
        self.store.settings["generation"]["max_concurrency"] = 1
        async with self.plugin._generation_slot():
            with self.short_deadline():
                await asyncio.wait_for(self.plugin.reply_images.decorate(event), 1)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.assertFalse(self.image_provider.calls)
        self.assertEqual(len(event.result.chain), 1)
        self.assertFalse(self.plugin._generation_waiters)

    async def test_late_result_from_provider_ignoring_cancel_never_attaches(self):
        event = self.event()
        self.enable(event)
        release = asyncio.Event()
        original = self.image_provider.generate
        async def delayed(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return await original(*args, **kwargs)
        self.image_provider.generate = delayed
        with self.short_deadline():
            await asyncio.wait_for(self.plugin.reply_images.decorate(event), 1)
        self.assertEqual(len(event.result.chain), 1)
        release.set()
        await asyncio.gather(*list(self.plugin._tasks), return_exceptions=True)
        self.assertEqual(len(event.result.chain), 1)
        self.assertFalse(self.context.sent)
