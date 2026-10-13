"""Bounded morning opportunities; isolated fake model and platform only."""
import asyncio
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import test_core as fixtures
from test_core import ActiveScheduler, Event


class MorningRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    def setup_morning(self):
        scheduler = ActiveScheduler(self.plugin)
        scheduler._in_window = lambda *_args: True
        self.store.settings["active"].update(enabled=False, min_idle_sec=0, min_gap_sec=60)
        self.store.settings["good_morning"].update(enabled=True, max_attempts=3, retry_interval_sec=900)
        self.store.settings["proactive_budget"].update(messages=10, llm=20, photos=10)
        scheduler.observe(Event())
        target = self.store.targets["items"][0]
        target.update(enabled=True, last_inbound=0, last_active=0, with_image=False)
        self.store.save_targets()
        date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        return scheduler, target, f"morning:{target['umo']}:{date}"

    def due(self, key):
        self.store.update_delivery(key, {"retry_at": time.time() - 1})

    async def skip_once(self):
        scheduler, target, key = self.setup_morning()
        self.context.llm.next = {"decision": "skip", "reply": ""}
        await scheduler.tick(wait=True)
        return scheduler, target, key

    async def test_skip_can_later_send_one_greeting_and_never_repeat(self):
        scheduler, target, key = await self.skip_once()
        first = self.store.delivery(key)
        self.assertTrue(first["retryable"])
        self.assertEqual(first["skip_reason"], "role_skip")
        self.assertGreaterEqual(first["retry_at"], time.time() + 898)
        diagnostic = (await self.plugin.web_diagnostics())["items"][0]
        self.assertIn("1/3", diagnostic["summary"])
        self.assertIn("900 秒", diagnostic["summary"])
        self.assertEqual(self.store.budget_summary(self.store.settings["proactive_budget"])["messages"]["used"], 0)
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)
        self.due(key)
        self.context.llm.next = {"decision": "chat", "reply": "早安，今天也一起好好生活吧。"}
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.store.delivery(key)["attempts"], 2)
        self.assertEqual(self.store.delivery(key)["status"], "sent")
        target["last_active"] = 0
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 2)
        self.assertEqual(len(self.context.sent), 1)

    async def test_skip_retries_stop_at_configured_limit(self):
        scheduler, _target, key = await self.skip_once()
        for _ in range(4):
            self.due(key)
            await scheduler.tick(wait=True)
        delivery = self.store.delivery(key)
        self.assertEqual(len(self.context.llm.calls), 3)
        self.assertEqual(delivery["attempts"], 3)
        self.assertFalse(delivery["retryable"])
        self.assertFalse(self.context.sent)
        summary = self.store.budget_summary(self.store.settings["proactive_budget"])
        self.assertEqual(summary["llm"]["used"], 3)
        self.assertEqual(summary["messages"]["used"], 0)

    async def test_single_attempt_mode_keeps_skip_terminal(self):
        scheduler, _target, key = self.setup_morning()
        self.store.settings["good_morning"]["max_attempts"] = 1
        self.context.llm.next = {"decision": "skip", "reply": ""}
        await scheduler.tick(wait=True)
        self.due(key)
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertFalse(self.store.delivery(key)["retryable"])

    async def test_explicit_role_refusal_is_not_retried(self):
        scheduler, _target, key = self.setup_morning()
        self.context.llm.next = {"decision": "refuse", "reply": "今天想安静一些。"}
        await scheduler.tick(wait=True)
        self.due(key)
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertEqual(self.store.delivery(key)["skip_reason"], "role_refuse")
        self.assertFalse(self.context.sent)

    async def test_new_inbound_ends_pending_morning_retry(self):
        scheduler, target, key = await self.skip_once()
        scheduler.observe(Event(text="早上好"))
        target["last_inbound"] -= 2
        self.due(key)
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertFalse(self.store.delivery(key)["retryable"])
        self.assertEqual(self.store.delivery(key)["skip_reason"], "user_replied")

    async def test_silent_refusal_is_terminal_not_a_technical_failure(self):
        scheduler, _target, key = self.setup_morning()
        self.context.llm.next = {"decision": "refuse", "reply": ""}
        await scheduler.tick(wait=True)
        self.assertEqual(self.store.delivery(key)["status"], "skipped")
        self.assertEqual(self.store.delivery(key)["skip_reason"], "role_refuse")
        self.due(key)
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertFalse(self.context.sent)

    async def test_retry_outside_window_or_while_silent_does_not_call_model(self):
        scheduler, target, key = await self.skip_once()
        self.due(key)
        scheduler._in_window = lambda *_args: False
        await scheduler.tick(wait=True)
        scheduler._in_window = lambda *_args: True
        target["silent_until"] = time.time() + 1000
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)

    async def test_retry_does_not_bypass_disabled_target_or_global_switch(self):
        scheduler, target, key = await self.skip_once()
        self.due(key)
        target["morning_enabled"] = False
        await scheduler.tick(wait=True)
        target["morning_enabled"] = True
        self.store.settings["good_morning"]["enabled"] = False
        await scheduler.tick(wait=True)
        self.store.settings["good_morning"]["enabled"] = True
        target["enabled"] = False
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)

    async def test_retry_obeys_shared_llm_budget(self):
        scheduler, _target, key = await self.skip_once()
        self.store.settings["proactive_budget"]["llm"] = 1
        self.due(key)
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)
        delivery = self.store.delivery(key)
        self.assertEqual(delivery["status"], "skipped")
        self.assertIn("预算", delivery["reason"])
        self.due(key)
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)

    async def test_separate_scheduler_instances_cannot_claim_same_retry(self):
        scheduler, _target, key = await self.skip_once()
        self.due(key)
        other = ActiveScheduler(self.plugin)
        other._in_window = lambda *_args: True
        self.context.llm.next = {"decision": "chat", "reply": "早安。"}
        await asyncio.gather(scheduler.tick(wait=True), other.tick(wait=True))
        self.assertEqual(len(self.context.llm.calls), 2)
        self.assertEqual(len(self.context.sent), 1)

    async def test_daily_active_skip_keeps_original_no_retry_policy(self):
        scheduler, target, _key = self.setup_morning()
        self.store.settings["good_morning"]["enabled"] = False
        self.store.settings["active"]["enabled"] = True
        self.context.llm.next = {"decision": "skip", "reply": ""}
        await scheduler.tick(wait=True)
        delivery = self.store.delivery(target["active_delivery_key"])
        self.assertFalse(delivery["retryable"])
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)

    async def test_legacy_skip_without_retry_metadata_remains_terminal(self):
        scheduler, _target, key = self.setup_morning()
        self.store.reserve_delivery(key)
        self.store.update_delivery(key, {"status": "skipped", "attempts": 1, "decision": {"decision": "skip", "reply": ""}})
        await scheduler.tick(wait=True)
        self.assertFalse(self.context.llm.calls)

    async def test_morning_settings_are_persisted_and_checked(self):
        await self.plugin.web_save_settings({"good_morning": {"max_attempts": 5, "retry_interval_sec": 60}})
        self.assertEqual(self.store.settings["good_morning"]["max_attempts"], 5)
        self.assertEqual(self.store.settings["good_morning"]["retry_interval_sec"], 60)
        for patch in ({"max_attempts": 0}, {"max_attempts": 6}, {"max_attempts": True}, {"max_attempts": 2.5}, {"max_attempts": float("nan")}, {"retry_interval_sec": 59}, {"retry_interval_sec": 3601}, {"retry_interval_sec": 90.5}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                await self.plugin.web_save_settings({"good_morning": patch})


if __name__ == "__main__":
    unittest.main()
