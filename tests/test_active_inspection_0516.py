"""Read-only opportunity explanations and stable independent model prompts."""
import copy
import json
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_core as fixtures
import test_proactive_time_0512 as clocks
from canvas_test.active import ActiveScheduler


class ActiveInspection0516Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    setup_clock = clocks.ProactiveTime0512Tests.setup_clock
    deliver = clocks.ProactiveTime0512Tests.deliver

    def setup_inspection(self, kind="morning"):
        scheduler, target = self.setup_clock(kind)
        # Keep the real common gate for preview-vs-scheduler comparisons.
        scheduler._blocked = types.MethodType(ActiveScheduler._blocked, scheduler)
        now = self.clock[0].timestamp()
        self.addCleanup(patch.stopall)
        patch("canvas_test.active.time.time", return_value=now).start()
        target.update(last_inbound=0, last_active=0, unanswered=0, silent_until=0)
        return scheduler, target, now

    def rows(self):
        return tuple(self.store._db.iterdump())

    async def test_inspection_is_read_only_and_explains_inherited_configuration(self):
        scheduler, target, now = self.setup_inspection()
        target.update(min_idle_sec=120, last_inbound=now - 30, with_image=False)
        self.store.save_targets()
        original = self.rows()
        self.plugin.dialogue.environment = AsyncMock(side_effect=AssertionError("preview must not create session"))
        self.context.llm.text_chat = AsyncMock(side_effect=AssertionError("preview must not call model"))
        self.context.send_message = AsyncMock(side_effect=AssertionError("preview must not send"))
        first = await scheduler.inspect_target(target)
        second = await scheduler.inspect_target(target)
        self.assertEqual(first, second)
        self.assertEqual(self.rows(), original)
        state = first["morning"]
        self.assertFalse(state["eligible"])
        self.assertEqual([item["code"] for item in state["blockers"]], ["idle"])
        self.assertEqual(state["next_check_at"], now + 90)
        self.assertEqual(state["effective"]["timezone"], {"value": "Asia/Hong_Kong", "source": "good_morning"})
        self.assertEqual(state["effective"]["min_idle_sec"], {"value": 120, "source": "target"})
        self.assertFalse(state["image_allowed"])
        self.assertIn("目标", state["image_reason"])
        self.assertIn("不保证", first["note"])

    async def test_expired_silence_preview_matches_scheduler_without_resetting_it(self):
        scheduler, target, now = self.setup_inspection()
        target.update(silent_until=now - 10, unanswered=20)
        self.store.save_targets()
        before = self.rows()
        result = await scheduler.inspect_target(target)
        self.assertTrue(result["morning"]["eligible"])
        self.assertEqual(self.rows(), before)
        self.assertEqual(target["unanswered"], 20)
        self.assertTrue(scheduler._still_allowed(target, "morning"))
        self.assertEqual(target["unanswered"], 0)
        self.assertEqual(target["silent_until"], 0)

    async def test_silence_threshold_preview_does_not_start_or_extend_silence(self):
        scheduler, target, now = self.setup_inspection()
        target["unanswered"] = 3
        self.store.save_targets()
        result = await scheduler.inspect_target(target)
        self.assertEqual(result["morning"]["blockers"][0]["code"], "silence_threshold")
        self.assertEqual(target["silent_until"], 0)
        self.assertTrue(scheduler._blocked(target, now))
        self.assertEqual(target["silent_until"], now + 86400)

    async def test_retry_preview_shares_rules_without_cancelling_the_delivery(self):
        scheduler, target, now = self.setup_inspection()
        key = f"morning:{target['umo']}:2026-10-19"
        self.store.reserve_delivery(key)
        self.store.update_delivery(key, {"status": "skipped", "attempts": 1, "retryable": True, "skip_reason": "role_skip", "inbound_at": 0, "retry_at": now + 900})
        result = (await scheduler.inspect_target(target))["morning"]
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["retry_at"], now + 900)
        self.assertEqual(result["blockers"][0]["code"], "retry_wait")
        self.assertFalse(scheduler._retry_ready(target, "morning", key, now))
        target["last_inbound"] = 1
        before = self.store.delivery(key)
        result = (await scheduler.inspect_target(target))["morning"]
        self.assertEqual(result["blockers"][0]["code"], "user_replied")
        self.assertIsNone(result["next_check_at"])
        self.assertIsNone(result["retry_at"])
        self.assertEqual(self.store.delivery(key), before)
        self.assertFalse(scheduler._retry_ready(target, "morning", key, now + 1000))
        self.assertEqual(self.store.delivery(key)["skip_reason"], "user_replied")

    async def test_window_budget_and_switch_blockers_are_all_visible(self):
        scheduler, target, _ = self.setup_inspection()
        scheduler._in_window = types.MethodType(lambda self, *args: ActiveScheduler._in_window(*args), scheduler)
        target.update(morning_start="09:00", morning_end="10:00", enabled=False)
        self.store.settings["proactive_budget"].update(messages=0, llm=0, photos=0)
        state = (await scheduler.inspect_target(target))["morning"]
        self.assertEqual({item["code"] for item in state["blockers"]}, {"target_disabled", "window", "budget_messages", "budget_llm"})
        self.assertEqual(state["effective"]["start"], {"value": "09:00", "source": "target"})
        self.assertFalse(state["image_allowed"])
        self.assertIsNone(state["next_check_at"])

    async def test_schedule_and_morning_priority_are_explained_without_spending(self):
        scheduler, target, _ = self.setup_inspection()
        self.store.settings["active"]["enabled"] = True
        scheduler._schedule_context = AsyncMock(return_value={"allowed": False, "status": "busy", "reason": "正在上课"})
        result = await scheduler.inspect_target(target)
        self.assertIn("morning_priority", {item["code"] for item in result["active"]["blockers"]})
        self.assertIn("schedule", {item["code"] for item in result["morning"]["blockers"]})
        self.assertFalse(self.context.llm.calls)
        self.assertFalse(self.context.sent)
        self.assertFalse(self.store.export()["deliveries"])

    async def test_persona_or_conversation_changes_without_schedule_block_old_text(self):
        for changed in ("astro_id", "cid"):
            with self.subTest(changed=changed):
                scheduler, target = self.setup_clock("active")
                original = self.plugin.dialogue.decide
                async def decision(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    self.plugin.dialogue.current_identity = AsyncMock(return_value={"astro_id": "other" if changed == "astro_id" else "shy", "cid": "other" if changed == "cid" else "conversation-1"})
                    return result
                self.plugin.dialogue.decide = decision
                self.key = "active:identity:" + changed
                await self.deliver()
                self.assertFalse(self.context.sent)
                self.assertEqual(self.store.delivery(self.key)["skip_reason"], "delivery_guard")
                self.plugin.dialogue.decide = original

    async def test_submitted_workflow_failure_never_enters_morning_regeneration(self):
        scheduler, target = self.setup_clock()
        self.context.llm.next = {"decision": "photo", "reply": "早安", "prompt": "portrait"}
        async def failed(*args, **kwargs):
            self.store.update_job(kwargs["job_id"], {"status": "failed", "recovery": {"submitted": True, "prompt_id": "one-original"}})
            raise TimeoutError("original workflow remains running")
        self.plugin._generate_job = failed
        await self.deliver()
        state = self.store.delivery(self.key)
        self.assertEqual(state["status"], "skipped")
        self.assertEqual(state["skip_reason"], "upstream_pending")
        self.assertFalse(state["retryable"])
        self.assertFalse(scheduler._retry_ready(target, "morning", self.key, time.time() + 2000))
        self.assertEqual(len(self.context.llm.calls), 1)

    async def test_conditions_changed_during_final_schedule_read_block_old_text(self):
        scheduler, target = self.setup_clock("active")
        calls = []
        async def schedule_read(_target):
            calls.append(True)
            if len(calls) == 2:
                session = self.store.list_sessions()[0]
                session["pending"] = {"request_id": "newer-conditions", "conditions": "暂时不发图"}
                self.store.save_session(session["umo"], session)
            return None
        scheduler._schedule_context = schedule_read
        await self.deliver()
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.delivery(self.key)["skip_reason"], "delivery_guard")


class DecisionPrompt0516Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    setup_clock = clocks.ProactiveTime0512Tests.setup_clock

    async def test_independent_system_is_stable_when_visual_time_and_schedule_change(self):
        scheduler, target = self.setup_clock()
        env = await self.plugin.dialogue.environment(target["umo"])
        env["schedule_context"] = "现在在阳台看书"
        env["proactive_context"] = {"time": scheduler._time_context(target, "morning"), "image_allowed": True, "morning_image_mode": "prefer_photo"}
        original = copy.deepcopy(env)
        await self.plugin.dialogue.decide(env, "早安机会", proactive=True, event_kind="早安", outfit="white dress")
        first = self.context.llm.calls[-1]
        self.assertEqual(env, original)
        env["persona"]["state"]["outfit"] = "blue shirt"
        env["session"]["pending"] = {"request_id": "new-agreement", "conditions": "远景"}
        env["schedule_context"] = "现在在窗边喝茶"
        env["proactive_context"]["time"]["now"] = "2026-10-19T08:45:00+08:00"
        await self.plugin.dialogue.decide(env, "另一次早安机会", proactive=True, event_kind="早安", outfit="coat")
        second = self.context.llm.calls[-1]
        self.assertEqual(first["system_prompt"], second["system_prompt"])
        for value in ("blue shirt", "new-agreement", "现在在窗边喝茶", "08:45", '"morning_outfit": "coat"'):
            self.assertIn(value, second["prompt"])
            self.assertNotIn(value, second["system_prompt"])
        self.assertNotIn("persona_canvas_photo", second["system_prompt"])
        self.assertEqual(len(self.context.llm.calls), 2)

    async def test_photo_decision_keeps_conditions_and_refusal_without_proactive_rules(self):
        env = await self.plugin.dialogue.environment("qq:FriendMessage:123")
        self.context.llm.next = {"decision": "ask", "reply": "远景可以吗？", "requirements": {"camera": "wide shot"}}
        result = await self.plugin.dialogue.decide(env, "给我自拍")
        request = self.context.llm.calls[-1]
        self.assertEqual(result["requirements"]["camera"], "wide shot")
        self.assertIn("等待用户下一轮确认", request["system_prompt"])
        self.assertNotIn("主动联系机会", request["system_prompt"])
        self.assertIn("shooting_preferences", request["prompt"])
        self.context.llm.next = {"decision": "refuse", "reply": "今天不想拍。"}
        self.assertEqual((await self.plugin.dialogue.decide(env, "给我自拍"))["decision"], "refuse")


if __name__ == "__main__":
    unittest.main()
