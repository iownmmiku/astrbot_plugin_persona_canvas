"""Frozen local clocks; fake model/platform, real generation storage and budgets."""
import copy
import importlib
import json
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_core as fixtures
from test_core import ActiveScheduler, Event


class ProactiveTime0512Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    def setup_clock(self, kind="morning", hour=8, minute=30):
        self.clock = [datetime(2026, 10, 19, hour, minute, tzinfo=ZoneInfo("Asia/Hong_Kong"))]
        clock = self.clock

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

        patcher = patch.object(importlib.import_module(ActiveScheduler.__module__), "datetime", Frozen)
        patcher.start()
        self.addCleanup(patcher.stop)
        scheduler = ActiveScheduler(self.plugin)
        scheduler.observe(Event())
        scheduler._blocked = lambda *_: False
        scheduler._in_window = lambda *_: True
        self.store.settings["active"].update(enabled=kind == "active", timezone="Asia/Hong_Kong")
        self.store.settings["good_morning"].update(enabled=kind == "morning", timezone="Asia/Hong_Kong", image_mode="prefer_photo")
        self.store.settings["proactive_budget"].update(messages=10, photos=10, llm=20)
        target = self.store.targets["items"][0]
        target.update(enabled=True, last_inbound=0, last_active=0, with_image=True)
        self.scheduler, self.target, self.kind = scheduler, target, kind
        self.key = "morning:test:2026-10-19" if kind == "morning" else "active:test"
        self.context.llm.next = {"decision": "chat", "reply": "早安，昨晚休息得怎么样？"}
        return scheduler, target

    async def deliver(self):
        await self.scheduler._deliver(self.target, self.kind, self.key, time.time(), "2026-10-19" if self.kind == "morning" else "")

    def photo(self):
        self.context.llm.next = {"decision": "photo", "reply": "早安，给你今天的照片。", "prompt": "morning portrait, soft sunlight", "state_patch": {"outfit": "blue dress"}}

    def payload(self):
        return self.store.delivery(self.key)["proactive_context"]

    async def test_overnight_history_receives_current_time_without_rewriting_history(self):
        self.setup_clock("active")
        self.target["last_inbound"] = (self.clock[0] - timedelta(hours=9)).timestamp()
        self.context.conversation_manager.history = [{"role": "user", "content": "今晚还有点事，等下再聊"}, {"role": "assistant", "content": "那我等你说晚安"}]
        history = copy.deepcopy(self.context.conversation_manager.history)
        await self.deliver()
        request = self.context.llm.calls[-1]
        self.assertEqual(request["contexts"], history)
        self.assertEqual(self.context.conversation_manager.history[:2], history)
        value = self.payload()["time"]
        self.assertEqual(value["now"], "2026-10-19T08:30:00+08:00")
        self.assertEqual(value["weekday"], "星期一")
        self.assertEqual(value["period"], "上午")
        self.assertEqual(value["last_inbound_at"], "2026-10-18T23:30:00+08:00")
        self.assertEqual(value["elapsed_seconds"], 9 * 3600)
        self.assertEqual(value["day_offset"], 1)
        self.assertIn('"now": "2026-10-19T08:30:00+08:00"', request["prompt"])
        self.assertNotIn('"now": "2026-10-19T08:30:00+08:00"', request["system_prompt"])
        self.assertIn("不构成今天白天补说晚安", request["system_prompt"])
        self.assertEqual(len(self.context.sent), 1)

    async def test_local_calendar_uses_target_timezone_and_kind_fallback(self):
        scheduler, target = self.setup_clock()
        target["timezone"] = "America/New_York"
        target["last_inbound"] = self.clock[0].timestamp() - 3600
        value = scheduler._time_context(target, "morning")
        self.assertEqual(value["now"], "2026-10-18T20:30:00-04:00")
        self.assertEqual(value["weekday"], "星期日")
        self.assertEqual(value["period"], "晚上")
        self.assertEqual(value["day_offset"], 0)
        target.pop("timezone")
        self.store.settings["active"]["timezone"] = "UTC"
        self.assertEqual(scheduler._time_context(target, "active")["now"], "2026-10-19T00:30:00+00:00")
        self.assertEqual(scheduler._time_context(target, "morning")["timezone"], "Asia/Hong_Kong")

    async def test_unknown_or_future_inbound_time_does_not_invent_1970_history(self):
        scheduler, target = self.setup_clock()
        for value in (0, None, "bad", float("nan"), float("inf"), -1, self.clock[0].timestamp() + 60):
            with self.subTest(value=value):
                target["last_inbound"] = value
                clock = scheduler._time_context(target, "morning")
                self.assertIsNone(clock["last_inbound_at"])
                self.assertIsNone(clock["elapsed_seconds"])
                self.assertIsNone(clock["day_offset"])

    async def test_prefer_photo_really_generates_sends_and_accounts_once(self):
        self.setup_clock()
        self.photo()
        await self.deliver()
        await self.deliver()
        self.assertEqual(self.payload()["morning_image_mode"], "prefer_photo")
        self.assertTrue(self.payload()["image_allowed"])
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual([part[0] for part in self.context.sent[0][1].chain], ["text", "image"])
        self.assertEqual(self.store.delivery(self.key)["status"], "sent")
        job, = self.store.recent_jobs()
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.assertEqual(job["source"], "morning")
        self.assertIn("已发送照片", self.context.conversation_manager.history[-1]["content"])
        budgets = self.store.budget_summary(self.store.settings["proactive_budget"])
        self.assertEqual([budgets[k]["used"] for k in ("messages", "photos", "llm")], [1, 1, 1])

    async def test_last_photo_budget_can_still_be_sent_after_its_reservation_is_used(self):
        self.setup_clock()
        self.store.settings["proactive_budget"]["photos"] = 1
        self.photo()
        await self.deliver()
        self.assertEqual(self.store.delivery(self.key)["status"], "sent")
        self.assertEqual(len(self.context.sent), 1)

    async def test_prefer_photo_respects_role_chat_skip_and_refusal(self):
        self.setup_clock()
        for index, choice in enumerate(("chat", "skip", "refuse")):
            self.key = "morning:choice:" + str(index)
            self.target.update(last_active=0, morning_date="")
            self.context.llm.next = {"decision": choice, "reply": "" if choice == "skip" else "今天先说早安。"}
            await self.deliver()
        self.assertEqual(len(self.context.llm.calls), 3)
        self.assertEqual(len(self.context.sent), 1)
        self.assertFalse(self.image_provider.calls)

    async def test_photo_gates_allow_text_and_never_force_image(self):
        self.setup_clock()
        for index, gate in enumerate(("target", "budget", "text_only", "manual_provider")):
            self.key = "morning:gate:" + str(index)
            self.target.update(with_image=True, last_active=0, morning_date="")
            self.store.settings["proactive_budget"]["photos"] = 10
            self.store.settings["good_morning"]["image_mode"] = "prefer_photo"
            self.image_provider.capabilities = fixtures.ProviderCapabilities()
            if gate == "target": self.target["with_image"] = False
            if gate == "budget": self.store.settings["proactive_budget"]["photos"] = 0
            if gate == "text_only": self.store.settings["good_morning"]["image_mode"] = "text_only"
            if gate == "manual_provider": self.image_provider.capabilities = fixtures.ProviderCapabilities(automated_generation=False)
            with self.subTest(gate=gate):
                await self.deliver()
                self.assertFalse(self.payload()["image_allowed"])
                self.assertTrue(self.payload()["image_unavailable_reason"])
                self.assertEqual(self.store.delivery(self.key)["status"], "sent")
        self.assertEqual(len(self.context.sent), 4)
        self.assertFalse(self.image_provider.calls)

    async def test_invalid_photo_decision_when_images_forbidden_is_terminal(self):
        self.setup_clock()
        self.target["with_image"] = False
        self.photo()
        await self.deliver()
        await self.deliver()
        self.assertEqual(self.store.delivery(self.key)["skip_reason"], "image_unavailable")
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.context.sent)

    async def test_auto_mode_can_choose_photo_and_text_only_does_not_limit_daily_photos(self):
        self.setup_clock()
        self.store.settings["good_morning"]["image_mode"] = "auto"
        self.photo()
        await self.deliver()
        self.assertEqual(self.payload()["morning_image_mode"], "auto")
        self.assertEqual(len(self.image_provider.calls), 1)
        self.kind, self.key = "active", "active:independent"
        self.store.settings["active"]["enabled"] = True
        self.store.settings["good_morning"]["image_mode"] = "text_only"
        self.target.update(last_active=0, morning_date="")
        await self.deliver()
        self.assertEqual(len(self.image_provider.calls), 2)
        self.assertEqual(len(self.context.sent), 2)

    async def test_preference_and_time_data_do_not_force_another_model_call(self):
        self.setup_clock()
        self.photo()
        await self.deliver()
        request = self.context.llm.calls[0]
        self.assertIn('"morning_image_mode": "prefer_photo"', request["prompt"])
        self.assertIn("prefer_photo 表示优先选择 photo", request["system_prompt"])
        self.assertIn("不强制拍照，不绕过角色意愿", request["system_prompt"])
        self.assertEqual(len(self.context.llm.calls), 1)

    async def test_photo_setting_changed_during_model_does_not_generate(self):
        self.setup_clock()
        self.photo()
        original = self.context.llm.text_chat
        async def change(**kwargs):
            result = await original(**kwargs)
            self.store.settings["good_morning"]["image_mode"] = "text_only"
            return result
        self.context.llm.text_chat = change
        await self.deliver()
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.context.sent)

    async def test_photo_setting_changed_during_generation_retains_original(self):
        self.setup_clock()
        self.photo()
        original = self.image_provider.generate
        async def change(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.target["with_image"] = False
            return result
        self.image_provider.generate = change
        await self.deliver()
        self.assertFalse(self.context.sent)
        job, = self.store.recent_jobs()
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.assertEqual(self.store.delivery(self.key)["skip_reason"], "image_unavailable")

    async def test_slow_model_cannot_send_across_date_period_timezone_or_age(self):
        self.setup_clock("active", hour=10, minute=59)
        original = self.context.llm.text_chat
        for index, change in enumerate(("date", "period", "timezone", "age", "clock_back")):
            self.clock[0] = datetime(2026, 10, 19, 10, 59, tzinfo=ZoneInfo("Asia/Hong_Kong"))
            self.target.pop("timezone", None)
            self.key = "active:expired:" + str(index)
            async def delayed(**kwargs):
                result = await original(**kwargs)
                if change == "date": self.clock[0] += timedelta(days=1)
                elif change == "period": self.clock[0] += timedelta(minutes=2)
                elif change == "timezone": self.target["timezone"] = "UTC"
                elif change == "age": self.clock[0] += timedelta(minutes=16)
                else: self.clock[0] -= timedelta(minutes=1)
                return result
            self.context.llm.text_chat = delayed
            with self.subTest(change=change):
                await self.deliver()
                self.assertEqual(self.store.delivery(self.key)["skip_reason"], "time_context_expired")
                self.assertFalse(self.store.delivery(self.key)["retryable"])
        self.assertFalse(self.context.sent)

    async def test_photo_expired_during_generation_keeps_asset_without_retry(self):
        self.setup_clock("active", hour=10, minute=59)
        self.photo()
        original = self.image_provider.generate
        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.clock[0] += timedelta(minutes=2)
            return result
        self.image_provider.generate = delayed
        await self.deliver()
        await self.deliver()
        job, = self.store.recent_jobs()
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.delivery(self.key)["skip_reason"], "time_context_expired")

    async def test_last_schedule_await_must_not_bypass_time_guard(self):
        self.setup_clock("active", hour=10, minute=59)
        calls = 0
        async def schedule(_):
            nonlocal calls
            calls += 1
            if calls == 2: self.clock[0] += timedelta(minutes=2)
            return None
        self.scheduler._schedule_context = schedule
        await self.deliver()
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.delivery(self.key)["skip_reason"], "time_context_expired")

    async def test_short_delay_in_same_period_sends_once(self):
        self.setup_clock("active")
        original = self.context.llm.text_chat
        async def delayed(**kwargs):
            result = await original(**kwargs)
            self.clock[0] += timedelta(minutes=2)
            return result
        self.context.llm.text_chat = delayed
        await self.deliver()
        self.assertEqual(self.store.delivery(self.key)["status"], "sent")
        self.assertEqual(len(self.context.sent), 1)

    async def test_morning_image_modes_persist_and_reject_invalid_values_atomically(self):
        for mode in ("prefer_photo", "auto", "text_only"):
            await self.plugin.web_save_settings({"good_morning": {"image_mode": mode}})
            self.assertEqual(self.store.settings["good_morning"]["image_mode"], mode)
        for mode in (None, True, 1, {}, [], "", "always"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                await self.plugin.web_save_settings({"good_morning": {"enabled": True, "image_mode": mode}})
            self.assertEqual(self.store.settings["good_morning"]["image_mode"], "text_only")
            self.assertFalse(self.store.settings["good_morning"]["enabled"])

