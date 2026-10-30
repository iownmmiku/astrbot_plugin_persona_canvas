"""Offline optional-plugin contracts; no real model, platform or bot accounts."""
import asyncio
import copy
import time
import types
import unittest

import test_core as fixtures
from test_core import ActiveScheduler, Event


class Schedule:
    def __init__(self):
        self.state = {"enabled": True, "applicable": True, "allowed": True, "status": "free", "persona_id": "shy", "cid": "conversation-1", "context_key": "shy:conversation-1:walk", "prompt": "当前活动：在河岸散步；半小时后回家。"}
        self.calls = []
        self.error = None

    async def get_proactive_context(self, umo):
        self.calls.append(umo)
        if self.error:
            raise self.error
        return copy.deepcopy(self.state)


class ScheduleLinkTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    def setup_link(self):
        scheduler = ActiveScheduler(self.plugin)
        scheduler._in_window = lambda *_args: True
        self.store.settings["active"].update(enabled=True, min_idle_sec=0, min_gap_sec=60)
        self.store.settings["good_morning"]["enabled"] = False
        scheduler.observe(Event())
        target = self.store.targets["items"][0]
        target.update(enabled=True, last_inbound=0, last_active=0, with_image=True)
        self.store.save_targets()
        self.context.llm.next = {"decision": "chat", "reply": "刚好沿河走走，想听听你今天怎么样。"}
        schedule = Schedule()
        self.context.get_registered_star = lambda name: types.SimpleNamespace(activated=True, star_cls=schedule) if name == "astrbot_plugin_persona_schedule" else None
        return scheduler, target, schedule

    async def deliver(self, scheduler, target, key="active:test"):
        await scheduler._deliver(target, "active", key, time.time())
        return self.store.delivery(key)

    def assert_no_active_spend(self):
        summary = self.store.budget_summary(self.store.settings["proactive_budget"])
        for kind in ("messages", "photos", "llm"):
            self.assertEqual(summary[kind]["used"], 0)
            self.assertEqual(summary[kind]["reserved"], 0)

    async def test_busy_cancels_without_model_image_message_or_delayed_retry(self):
        scheduler, target, schedule = self.setup_link()
        schedule.state.update(allowed=False, status="busy", reason="正在上课")
        self.assertEqual((await self.deliver(scheduler, target))["status"], "skipped")
        self.assertFalse(self.context.llm.calls)
        self.assertFalse(self.context.sent)
        self.assertFalse(self.image_provider.calls)
        self.assert_no_active_spend()
        schedule.state.update(allowed=True, status="free")
        self.assertEqual((await self.deliver(scheduler, target))["status"], "skipped")
        self.assertEqual(len(schedule.calls), 1)
        self.assertFalse(self.context.sent)

    async def test_free_and_slow_use_schedule_in_role_decision_and_photo_prompt(self):
        for status in ("free", "slow"):
            with self.subTest(status=status):
                scheduler, target, schedule = self.setup_link()
                schedule.state["status"] = status
                self.context.llm.next = {"decision": "photo", "reply": "给你看看河边。", "prompt": "portrait at riverside", "state_patch": {}}
                state = await self.deliver(scheduler, target, "active:" + status)
                self.assertEqual(state["status"], "sent")
                self.assertIn(schedule.state["prompt"], self.context.llm.calls[-1]["prompt"])
                self.assertNotIn(schedule.state["prompt"], self.context.llm.calls[-1]["system_prompt"])
                self.assertIn(schedule.state["prompt"], self.image_provider.calls[-1]["positive"])
                self.assertIn("do not render this as text", self.image_provider.calls[-1]["positive"])
                self.assertEqual(len(schedule.calls), 3)

    async def test_disabled_or_outside_whitelist_keeps_original_behaviour(self):
        scheduler, target, schedule = self.setup_link()
        schedule.state.update(enabled=False, applicable=False, allowed=False, status="busy")
        self.assertEqual((await self.deliver(scheduler, target))["status"], "sent")
        self.assertNotIn(schedule.state["prompt"], self.context.llm.calls[-1]["system_prompt"])

    async def test_absent_disabled_and_old_plugin_keep_original_behaviour(self):
        for metadata in (None, types.SimpleNamespace(activated=False, star_cls=Schedule()), types.SimpleNamespace(activated=True, star_cls=object())):
            with self.subTest(metadata=metadata):
                scheduler, target, _schedule = self.setup_link()
                self.context.get_registered_star = lambda _name: metadata
                state = await self.deliver(scheduler, target, "active:" + str(len(self.context.sent)))
                self.assertEqual(state["status"], "sent")

    async def test_busy_after_decision_cancels_before_image_generation(self):
        scheduler, target, schedule = self.setup_link()
        async def decide(*_args, **_kwargs):
            schedule.state.update(allowed=False, status="busy")
            return {"decision": "photo", "reply": "看看河边。", "prompt": "riverside", "state_patch": {}}
        self.plugin.dialogue.decide = decide
        self.assertEqual((await self.deliver(scheduler, target))["status"], "skipped")
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.budget_summary(self.store.settings["proactive_budget"])["messages"]["used"], 0)

    async def test_changed_schedule_persona_or_conversation_cancels_old_text(self):
        for change in ("new-slot", "new-persona", "new-conversation", "new-day"):
            with self.subTest(change=change):
                scheduler, target, schedule = self.setup_link()
                async def decide(*_args, **_kwargs):
                    schedule.state["context_key"] = change
                    return {"decision": "chat", "reply": "过时的活动内容", "state_patch": {}}
                self.plugin.dialogue.decide = decide
                self.assertEqual((await self.deliver(scheduler, target, "active:" + change))["status"], "skipped")
                self.assertFalse(self.context.sent)

    async def test_busy_during_image_generation_cancels_photo_and_disallows_resend(self):
        scheduler, target, schedule = self.setup_link()
        self.context.llm.next = {"decision": "photo", "reply": "看看河边。", "prompt": "riverside", "state_patch": {}}
        generate = self.image_provider.generate
        async def change_while_generating(*args, **kwargs):
            result = await generate(*args, **kwargs)
            schedule.state.update(allowed=False, status="busy")
            return result
        self.image_provider.generate = change_while_generating
        state = await self.deliver(scheduler, target)
        job = self.store.job(state["job_id"])
        self.assertEqual(state["status"], "skipped")
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(job["cancel_requested"])
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.assertFalse(self.context.sent)
        with self.assertRaises(ValueError):
            await self.plugin.web_resend_job({"id": job["id"], "confirm_uncertain": True})
        self.assertEqual(self.store.budget_summary(self.store.settings["proactive_budget"])["messages"]["used"], 0)

    async def test_changed_activity_after_image_generation_cancels_saved_photo(self):
        scheduler, target, schedule = self.setup_link()
        self.context.llm.next = {"decision": "photo", "reply": "看看河边。", "prompt": "riverside", "state_patch": {}}
        generate = self.image_provider.generate
        async def change_while_generating(*args, **kwargs):
            result = await generate(*args, **kwargs)
            schedule.state["context_key"] = "back-home"
            return result
        self.image_provider.generate = change_while_generating
        state = await self.deliver(scheduler, target)
        self.assertEqual(state["status"], "skipped")
        self.assertEqual(self.store.job(state["job_id"])["status"], "cancelled")
        self.assertFalse(self.context.sent)

    async def test_first_schedule_error_is_safe_skipped_and_next_opportunity_can_recover(self):
        scheduler, target, schedule = self.setup_link()
        schedule.error = RuntimeError("token=should-not-appear")
        state = await self.deliver(scheduler, target)
        self.assertEqual(state["status"], "skipped")
        self.assertNotIn("should-not-appear", str(self.store.recent_actions()) + str(state))
        self.assert_no_active_spend()
        schedule.error = None
        self.assertEqual((await self.deliver(scheduler, target, "active:recovered"))["status"], "sent")

    async def test_schedule_error_after_model_call_never_sends_old_reply(self):
        scheduler, target, schedule = self.setup_link()
        async def decide(*_args, **_kwargs):
            schedule.error = RuntimeError("private-provider-details")
            return {"decision": "chat", "reply": "不应发送", "state_patch": {}}
        self.plugin.dialogue.decide = decide
        state = await self.deliver(scheduler, target)
        self.assertEqual(state["status"], "skipped")
        self.assertNotIn("private-provider-details", str(state))
        self.assertFalse(self.context.sent)

    async def test_malformed_schedule_response_is_skipped(self):
        for state in (None, {}, {"applicable": "yes"}, {"applicable": True, "allowed": "yes"}, {"applicable": True, "allowed": True}, {"applicable": True, "allowed": True, "context_key": "busy", "status": "busy"}):
            with self.subTest(state=state):
                scheduler, target, schedule = self.setup_link()
                schedule.state = state
                result = await self.deliver(scheduler, target, "active:invalid:" + str(len(schedule.calls)) + str(state))
                self.assertEqual(result["status"], "skipped")
                self.assertFalse(self.context.sent)

    async def test_async_discovery_and_plugin_reload_are_resolved_each_time(self):
        scheduler, target, schedule = self.setup_link()
        async def discover(_name):
            return types.SimpleNamespace(activated=True, star_cls=schedule)
        self.context.get_registered_star = discover
        self.assertEqual((await self.deliver(scheduler, target))["status"], "sent")
        schedule = Schedule()
        schedule.state.update(allowed=False, status="busy")
        self.assertEqual((await self.deliver(scheduler, target, "active:reloaded"))["status"], "skipped")
        self.assertEqual(len(self.context.sent), 1)

    async def test_cancelled_lookup_propagates_plugin_shutdown(self):
        scheduler, target, schedule = self.setup_link()
        schedule.error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.deliver(scheduler, target)
        self.assertFalse(self.context.sent)
        self.assert_no_active_spend()

    async def test_link_disabled_or_plugin_removed_after_decision_cancels_current_opportunity(self):
        for mode in ("disabled", "removed"):
            with self.subTest(mode=mode):
                scheduler, target, schedule = self.setup_link()
                async def decide(*_args, **_kwargs):
                    if mode == "disabled":
                        schedule.state["applicable"] = False
                    else:
                        self.context.get_registered_star = lambda _name: None
                    return {"decision": "chat", "reply": "旧日程内容", "state_patch": {}}
                self.plugin.dialogue.decide = decide
                self.assertEqual((await self.deliver(scheduler, target, "active:" + mode))["status"], "skipped")
                self.assertFalse(self.context.sent)

    async def test_initial_astrbot_persona_and_conversation_must_match_schedule(self):
        for patch in ({"persona_id": "other-persona"}, {"cid": "another-conversation"}):
            with self.subTest(patch=patch):
                scheduler, target, schedule = self.setup_link()
                schedule.state.update(patch)
                self.assertEqual((await self.deliver(scheduler, target, "active:" + str(patch)))["status"], "skipped")
                self.assertFalse(self.context.llm.calls)
                self.assertFalse(self.context.sent)
                self.assert_no_active_spend()

    async def test_modern_persona_resolver_uses_platform_and_provider_settings(self):
        scheduler, target, schedule = self.setup_link()
        selected = []
        async def resolve(**kwargs):
            selected.append(kwargs)
            return "real-default", {"name": "real-default", "prompt": "现代解析后的角色"}, None, False
        self.context.persona_manager.resolve_selected_persona = resolve
        self.context.get_config = lambda _umo: {"provider_settings": {"custom": "runner-default"}}
        self.context.get_platform_inst = lambda _id: types.SimpleNamespace(meta=lambda: types.SimpleNamespace(name="aiocqhttp"))
        schedule.state.update(persona_id="real-default")
        self.assertEqual((await self.deliver(scheduler, target))["status"], "sent")
        self.assertIn("现代解析后的角色", self.context.llm.calls[-1]["system_prompt"])
        self.assertEqual(selected[0]["platform_name"], "aiocqhttp")
        self.assertEqual(selected[0]["provider_settings"], {"custom": "runner-default"})
        self.assertEqual(selected[0]["conversation_persona_id"], "shy")

    async def test_modern_no_persona_sentinel_does_not_use_legacy_persona(self):
        _scheduler, target, _schedule = self.setup_link()
        async def resolve(**_kwargs):
            return "[%None]", None, "[%None]", False
        self.context.persona_manager.resolve_selected_persona = resolve
        env = await self.plugin.dialogue.environment(target["umo"])
        self.assertEqual(env["astro_id"], "[%None]")
        self.assertEqual(env["system"], "")

    async def test_current_conversation_switch_cancels_even_if_schedule_snapshot_is_unchanged(self):
        scheduler, target, _schedule = self.setup_link()
        async def decide(*_args, **_kwargs):
            self.context.conversation_manager.cid = "new-current-conversation"
            return {"decision": "chat", "reply": "旧会话的内容", "state_patch": {}}
        self.plugin.dialogue.decide = decide
        self.assertEqual((await self.deliver(scheduler, target))["status"], "skipped")
        self.assertFalse(self.context.sent)

    async def test_current_persona_switch_cancels_even_if_schedule_snapshot_is_unchanged(self):
        scheduler, target, _schedule = self.setup_link()
        selected = {"id": "shy"}
        async def resolve(**_kwargs):
            return selected["id"], {"name": selected["id"], "prompt": "当前人设"}, None, False
        self.context.persona_manager.resolve_selected_persona = resolve
        async def decide(*_args, **_kwargs):
            selected["id"] = "new-persona"
            return {"decision": "chat", "reply": "旧人设的内容", "state_patch": {}}
        self.plugin.dialogue.decide = decide
        self.assertEqual((await self.deliver(scheduler, target))["status"], "skipped")
        self.assertFalse(self.context.sent)

    async def test_current_identity_failure_uses_fixed_diagnostic(self):
        scheduler, target, _schedule = self.setup_link()
        async def fail(_umo):
            raise RuntimeError("secret-provider-credential")
        self.plugin.dialogue.current_identity = fail
        result = await self.deliver(scheduler, target)
        self.assertEqual(result["status"], "skipped")
        self.assertNotIn("secret-provider-credential", str(result) + str(self.store.recent_actions()))
        self.assertFalse(self.context.sent)

    async def test_current_identity_cancellation_propagates(self):
        scheduler, target, _schedule = self.setup_link()
        async def cancel(_umo):
            raise asyncio.CancelledError()
        self.plugin.dialogue.current_identity = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.deliver(scheduler, target)
        self.assertFalse(self.context.sent)

    async def test_schedule_becomes_busy_while_identity_lookup_waits(self):
        scheduler, target, schedule = self.setup_link()
        lookup = self.plugin.dialogue.current_identity
        async def update_during_identity(umo):
            identity = await lookup(umo)
            await asyncio.sleep(0)
            schedule.state.update(allowed=False, status="busy", reason="时段已改为忙碌")
            return identity
        self.plugin.dialogue.current_identity = update_during_identity
        result = await self.deliver(scheduler, target)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "时段已改为忙碌")
        self.assertFalse(self.context.sent)

    async def test_identity_check_does_not_create_or_modify_visual_session(self):
        _scheduler, target, _schedule = self.setup_link()
        def reject_mutation(*_args, **_kwargs):
            raise AssertionError("Identity check attempted a session write")
        self.store.session = reject_mutation
        self.store.save_session = reject_mutation
        self.assertEqual(await self.plugin.dialogue.current_identity(target["umo"]), {"astro_id": "shy", "cid": "conversation-1"})

    async def test_link_enabled_during_decision_cancels_unlinked_content(self):
        scheduler, target, schedule = self.setup_link()
        schedule.state["applicable"] = False
        async def decide(*_args, **_kwargs):
            schedule.state["applicable"] = True
            return {"decision": "chat", "reply": "没有参考日程的内容", "state_patch": {}}
        self.plugin.dialogue.decide = decide
        self.assertEqual((await self.deliver(scheduler, target))["status"], "skipped")
        self.assertFalse(self.context.sent)

    async def test_link_disabled_during_image_generation_cancels_saved_photo(self):
        scheduler, target, schedule = self.setup_link()
        self.context.llm.next = {"decision": "photo", "reply": "看看河边。", "prompt": "riverside", "state_patch": {}}
        generate = self.image_provider.generate
        async def disable_during_generation(*args, **kwargs):
            result = await generate(*args, **kwargs)
            schedule.state["applicable"] = False
            return result
        self.image_provider.generate = disable_during_generation
        result = await self.deliver(scheduler, target)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.store.job(result["job_id"])["status"], "cancelled")
        self.assertFalse(self.context.sent)

    async def test_observed_platform_name_is_used_by_modern_persona_resolver(self):
        scheduler, target, _schedule = self.setup_link()
        event = Event()
        event.get_platform_name = lambda: "aiocqhttp"
        scheduler.observe(event)
        selected = []
        async def resolve(**kwargs):
            selected.append(kwargs)
            return "shy", {"name": "shy", "prompt": "当前人设"}, None, False
        self.context.persona_manager.resolve_selected_persona = resolve
        await self.plugin.dialogue.current_identity(target["umo"])
        self.assertEqual(selected[0]["platform_name"], "aiocqhttp")

    async def test_morning_busy_opportunity_is_not_postponed_until_free(self):
        scheduler, target, schedule = self.setup_link()
        self.store.settings["active"]["enabled"] = False
        self.store.settings["good_morning"]["enabled"] = True
        schedule.state.update(allowed=False, status="busy")
        await scheduler.tick(wait=True)
        schedule.state.update(allowed=True, status="free")
        await scheduler.tick(wait=True)
        self.assertFalse(self.context.sent)
        self.assertEqual(len(schedule.calls), 1)


if __name__ == "__main__":
    unittest.main()
