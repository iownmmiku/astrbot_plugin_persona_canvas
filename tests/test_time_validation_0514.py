"""Real local-clock validation; offline model, platform and image provider IO."""
import copy
import json
import types
import unittest
from datetime import timedelta

import test_core as fixtures
import test_proactive_time_0512 as clocks
from canvas_test.temporal import temporal_conflicts, time_brief


class TimeValidation0514Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    setup_clock = clocks.ProactiveTime0512Tests.setup_clock
    deliver = clocks.ProactiveTime0512Tests.deliver

    def sequence(self, first, second=None, change=None):
        self.calls = []
        async def reply(**kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 2 and change:
                change()
            value = first if len(self.calls) == 1 else second or first
            if isinstance(value, Exception):
                raise value
            return types.SimpleNamespace(completion_text=json.dumps(value, ensure_ascii=False))
        self.context.llm.text_chat = reply

    async def test_explicit_time_conflicts_are_detected_against_local_clock(self):
        scheduler, target = self.setup_clock()
        clock = scheduler._time_context(target, "morning")
        for text in ("主人，晚安～", "现在是晚上，早点休息", "现在下午三点", "现在是03:00", "现在三点半", "今天是2026年10月18日", "今天是10月18号", "今天是星期日", "今天周六", "现在25点了"):
            with self.subTest(text=text):
                self.assertTrue(temporal_conflicts(text, clock))
        for text in ("早安", "现在是上午", "现在八点半", "现在08:30", "今天是2026年10月19日", "今天是10月19号", "今天是星期一"):
            with self.subTest(text=text):
                self.assertFalse(temporal_conflicts(text, clock))

    async def test_history_questions_quotes_negations_and_future_plans_are_not_greetings(self):
        scheduler, target = self.setup_clock()
        clock = scheduler._time_context(target, "morning")
        for text in ("昨晚没说晚安，今天休息好吗？", "明天早安时再见", "今晚再说晚安", "晚上说晚安，早上说早安", "还不想说晚安", "不要说晚安", "晚安是什么意思", "你会说晚安吗？", "现在是下午吗", "现在是晚上？", "“现在是下午，晚安”是昨晚的话", "'晚安'这个词", "```今天星期日，晚安```", "> 现在是晚上，晚安", "明天再见，你昨晚休息如何？", "昨晚你说现在是晚上", "如果现在是晚上我会说晚安", "待会儿说晚安", "你以为今天是星期日"):
            with self.subTest(text=text):
                self.assertFalse(temporal_conflicts(text, clock))

    async def test_custom_morning_window_does_not_override_factual_clock_claims(self):
        scheduler, target = self.setup_clock(hour=23)
        clock = scheduler._time_context(target, "morning")
        self.assertFalse(temporal_conflicts("早安，来看看你", clock, morning=True))
        self.assertTrue(temporal_conflicts("早安，现在是上午八点", clock, morning=True))
        self.assertFalse(temporal_conflicts("晚安", clock))

    async def test_human_clock_header_and_sent_history_have_real_timestamps(self):
        self.setup_clock()
        self.context.llm.next = {"decision": "chat", "reply": "早安！"}
        before = copy.deepcopy(self.context.conversation_manager.history)
        await self.deliver()
        request = self.context.llm.calls[0]
        self.assertTrue(request["prompt"].startswith("本次读取的真实当地时间：2026年10月19日 08:30:00"))
        self.assertIn("Asia/Hong_Kong", request["prompt"])
        self.assertEqual(request["contexts"], before)
        self.assertEqual(self.context.conversation_manager.history[:len(before)], before)
        self.assertIn("2026-10-19T08:30:00+08:00", self.context.conversation_manager.history[-2]["content"])
        self.assertEqual(len(self.context.llm.calls), 1)

    async def test_wrong_daytime_goodnight_gets_one_history_free_correction(self):
        self.setup_clock()
        self.sequence({"decision": "chat", "reply": "晚安，早点休息。"}, {"decision": "chat", "reply": "早安，昨晚休息得好吗？"})
        await self.deliver()
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[0]["contexts"])
        self.assertEqual(self.calls[1]["contexts"], [])
        self.assertEqual(len(self.context.sent), 1)
        self.assertIn("早安", self.context.sent[0][1].chain[0][1])
        self.assertEqual(self.store.delivery(self.key)["time_review"]["status"], "corrected")
        budget = self.store.budget_summary(self.store.settings["proactive_budget"])
        self.assertEqual(budget["llm"]["used"], 2)
        self.assertEqual(budget["messages"]["used"], 1)

    async def test_still_wrong_after_correction_is_terminal_and_never_sent(self):
        self.setup_clock()
        self.sequence({"decision": "chat", "reply": "现在晚上三点，晚安"})
        await self.deliver()
        await self.deliver()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.delivery(self.key)["skip_reason"], "time_conflict")
        self.assertFalse(self.store.delivery(self.key)["retryable"])
        self.assertTrue(self.store.delivery(self.key)["time_review"]["remaining_issues"])

    async def test_correction_budget_exhausted_never_sends_original(self):
        self.setup_clock()
        self.store.settings["proactive_budget"]["llm"] = 1
        self.sequence({"decision": "chat", "reply": "晚安"})
        await self.deliver()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.context.sent)
        self.assertIn("预算不足", self.store.delivery(self.key)["reason"])

    async def test_correction_failure_is_skip_not_generation_retry(self):
        self.setup_clock()
        self.sequence({"decision": "chat", "reply": "晚安"}, ValueError("offline correction error"))
        await self.deliver()
        await self.deliver()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.delivery(self.key)["status"], "skipped")
        self.assertFalse(self.store.recent_jobs())

    async def test_correction_cannot_upgrade_text_to_a_paid_photo(self):
        self.setup_clock()
        self.sequence({"decision": "chat", "reply": "晚安"}, {"decision": "photo", "reply": "早安", "prompt": "morning portrait"})
        await self.deliver()
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.context.sent)
        self.assertIn("扩大原动作", self.store.delivery(self.key)["reason"])

    async def test_photo_time_can_be_corrected_once_without_losing_conditions(self):
        self.setup_clock()
        self.sequence({"decision": "photo", "reply": "晚安，这是现在的照片", "prompt": "night portrait", "requirements": {"outfit": "white dress", "camera": "wide shot", "avoid": "close-up"}}, {"decision": "photo", "reply": "早安，这是今天的照片", "prompt": "morning portrait", "requirements": {"outfit": "swimsuit"}})
        await self.deliver()
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(len(self.context.sent), 1)
        job, = self.store.recent_jobs()
        self.assertEqual(job["requirements"]["outfit"], "white dress")
        self.assertIn("wide shot", self.image_provider.calls[0]["positive"])
        self.assertIn("close-up", self.image_provider.calls[0]["negative"])

    async def test_role_can_skip_or_refuse_time_correction_without_photo(self):
        self.setup_clock()
        self.sequence({"decision": "photo", "reply": "晚安", "prompt": "portrait"}, {"decision": "refuse", "reply": ""})
        await self.deliver()
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.delivery(self.key)["skip_reason"], "role_refuse")

    async def test_new_message_during_correction_stops_send(self):
        self.setup_clock()
        self.sequence({"decision": "chat", "reply": "晚安"}, {"decision": "chat", "reply": "早安"}, lambda: self.target.update(last_inbound=123))
        await self.deliver()
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.delivery(self.key)["status"], "skipped")

    async def test_image_disabled_during_correction_stops_generation(self):
        self.setup_clock()
        self.sequence({"decision": "photo", "reply": "晚安", "prompt": "night portrait"}, {"decision": "photo", "reply": "早安", "prompt": "morning portrait"}, lambda: self.target.update(with_image=False))
        await self.deliver()
        self.assertFalse(self.context.sent)
        self.assertFalse(self.image_provider.calls)

    async def test_calendar_change_during_correction_stops_old_day_send(self):
        self.setup_clock()
        def cross_day():
            self.clock[0] += timedelta(days=1)
        self.sequence({"decision": "chat", "reply": "晚安"}, {"decision": "chat", "reply": "早安"}, cross_day)
        await self.deliver()
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.delivery(self.key)["skip_reason"], "date_changed")

    async def test_generated_photo_with_expired_exact_time_retains_asset_without_retry(self):
        self.setup_clock()
        self.context.llm.next = {"decision": "photo", "reply": "现在08:30，给你照片", "prompt": "morning portrait"}
        original = self.image_provider.generate
        async def generate(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.clock[0] += timedelta(minutes=10)
            return result
        self.image_provider.generate = generate
        await self.deliver()
        await self.deliver()
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertFalse(self.context.sent)
        job, = self.store.recent_jobs()
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.assertEqual(self.store.delivery(self.key)["skip_reason"], "time_conflict")

    async def test_clock_api_shows_separate_inherited_timezones_without_persisting_snapshot(self):
        self.setup_clock()
        self.store.settings["active"]["timezone"] = "UTC"
        items = (await self.plugin.web_targets())["items"]
        self.assertEqual(items[0]["clock"]["active"]["now"], "2026-10-19T00:30:00+00:00")
        self.assertEqual(items[0]["clock"]["morning"]["now"], "2026-10-19T08:30:00+08:00")
        self.assertNotIn("clock", self.store.targets["items"][0])
        await self.plugin.web_target_action({"action": "save", "umo": self.target["umo"], "patch": {"timezone": "UTC"}})
        await self.plugin.web_target_action({"action": "save", "umo": self.target["umo"], "patch": {"timezone": ""}})
        self.assertEqual(self.target["timezone"], "")

    async def test_clock_header_distinguishes_overnight_delay_from_current_time(self):
        scheduler, target = self.setup_clock()
        target["last_inbound"] = (self.clock[0] - timedelta(hours=10)).timestamp()
        text = time_brief(scheduler._time_context(target, "morning"))
        self.assertIn("10 小时 0 分钟", text)
        self.assertIn("相隔 1 个当地日期", text)
        self.assertIn("08:30:00", text)
