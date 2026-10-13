"""Fake-clock regressions for daily early greeting ownership across midnight."""
import asyncio
import importlib
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_core as fixtures
from test_core import ActiveScheduler, Event


class MorningDateTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    def setup_morning(self):
        scheduler = ActiveScheduler(self.plugin)
        self.store.settings["active"].update(enabled=False, min_idle_sec=0, min_gap_sec=60)
        self.store.settings["good_morning"].update(enabled=True, start="23:30", end="01:00", max_attempts=3, retry_interval_sec=900)
        self.store.settings["proactive_budget"].update(messages=10, llm=20, photos=10)
        scheduler.observe(Event())
        target = self.store.targets["items"][0]
        target.update(enabled=True, last_inbound=0, last_active=0, with_image=False)
        self.store.save_targets()
        self.calendar = [datetime(2026, 10, 13, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))]
        calendar = self.calendar

        class CalendarDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return calendar[0].astimezone(tz) if tz else calendar[0].replace(tzinfo=None)

        module = importlib.import_module(ActiveScheduler.__module__)
        return scheduler, target, patch.object(module, "datetime", CalendarDatetime)

    @staticmethod
    def decision(photo=False):
        return {"decision": "photo" if photo else "chat", "reply": "早安，今天也一起好好生活吧。", "prompt": "portrait, blue dress" if photo else "", "state_patch": {}, "requirements": {}}

    async def until(self, condition):
        async def poll():
            while not condition():
                await asyncio.sleep(.001)
        await asyncio.wait_for(poll(), 2)

    async def settle(self, scheduler):
        while scheduler._target_tasks:
            await asyncio.gather(*list(scheduler._target_tasks.values()), return_exceptions=True)
            await asyncio.sleep(0)

    async def test_normal_same_day_sends_once(self):
        scheduler, target, fake_calendar = self.setup_morning()
        self.context.llm.next = self.decision()
        with fake_calendar:
            await scheduler.tick(wait=True)
            target["last_active"] = 0
            await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertEqual(target["morning_date"], "2026-10-13")

    async def test_skip_retry_crossing_midnight_is_not_sent_and_new_day_can_send(self):
        scheduler, target, fake_calendar = self.setup_morning()
        old_key = f"morning:{target['umo']}:2026-10-13"
        entered, release = asyncio.Event(), asyncio.Event()

        async def decide(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return self.decision()

        with fake_calendar:
            self.context.llm.next = {"decision": "skip", "reply": ""}
            await scheduler.tick(wait=True)
            self.store.update_delivery(old_key, {"retry_at": 0})
            self.plugin.dialogue.decide = decide
            await scheduler.tick()
            await asyncio.wait_for(entered.wait(), 2)
            self.calendar[0] += timedelta(minutes=2)
            release.set()
            await self.settle(scheduler)
            self.assertFalse(self.context.sent)
            self.assertEqual(self.store.delivery(old_key)["status"], "skipped")
            self.assertFalse(target["morning_date"])
            await scheduler.tick(wait=True)
            target["last_active"] = 0
            await scheduler.tick(wait=True)
        new_key = f"morning:{target['umo']}:2026-10-14"
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.store.delivery(new_key)["status"], "sent")
        self.assertEqual(target["morning_date"], "2026-10-14")

    async def test_photo_crossing_midnight_is_retained_without_old_day_delivery(self):
        scheduler, target, fake_calendar = self.setup_morning()
        target["with_image"] = True
        self.context.llm.next = self.decision(photo=True)
        release = asyncio.Event()
        self.image_provider.pause = release
        old_key = f"morning:{target['umo']}:2026-10-13"
        with fake_calendar:
            await scheduler.tick()
            await self.until(lambda: bool(self.image_provider.calls))
            self.calendar[0] += timedelta(minutes=2)
            release.set()
            await self.settle(scheduler)
            self.assertFalse(self.context.sent)
            self.assertEqual(self.store.delivery(old_key)["status"], "skipped")
            job, = self.store.recent_jobs()
            self.assertEqual(job["status"], "succeeded")
            self.assertTrue(self.store.asset(job["asset"]).is_file())
            self.context.llm.next = self.decision()
            await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(target["morning_date"], "2026-10-14")

    async def test_expired_day_is_rejected_before_claim_or_model_call(self):
        scheduler, target, fake_calendar = self.setup_morning()
        old_key = f"morning:{target['umo']}:2026-10-13"
        self.calendar[0] += timedelta(minutes=2)
        with fake_calendar:
            await scheduler._deliver(target, "morning", old_key, 0, "2026-10-13")
        self.assertIsNone(self.store.delivery(old_key))
        self.assertFalse(self.context.llm.calls)
        self.assertFalse(self.context.sent)

    async def test_calendar_guard_uses_target_timezone(self):
        scheduler, target, fake_calendar = self.setup_morning()
        target.update(timezone="UTC", morning_start="15:30", morning_end="17:00")
        entered, release = asyncio.Event(), asyncio.Event()

        async def decide(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return self.decision()

        self.plugin.dialogue.decide = decide
        with fake_calendar:
            await scheduler.tick()
            await asyncio.wait_for(entered.wait(), 2)
            # Shanghai crosses midnight, but the target's UTC calendar does not.
            self.calendar[0] += timedelta(minutes=2)
            release.set()
            await self.settle(scheduler)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(target["morning_date"], "2026-10-13")


if __name__ == "__main__":
    unittest.main()
