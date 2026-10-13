"""A schedule cancellation must never become a morning role-skip retry."""
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import test_core as fixtures
import test_schedule_link as schedule_fixtures


class MorningScheduleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    def setup_morning(self):
        scheduler, target, schedule = schedule_fixtures.ScheduleLinkTests.setup_link(self)
        self.store.settings["active"]["enabled"] = False
        self.store.settings["good_morning"].update(enabled=True, max_attempts=3, retry_interval_sec=900)
        self.context.llm.next = {"decision": "skip", "reply": ""}
        date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        return scheduler, target, schedule, f"morning:{target['umo']}:{date}"

    async def test_schedule_becomes_busy_during_role_skip_stops_morning_retry(self):
        scheduler, _target, schedule, key = self.setup_morning()
        original = self.plugin.dialogue.decide

        async def decide(*args, **kwargs):
            result = await original(*args, **kwargs)
            schedule.state.update(allowed=False, status="busy", reason="正在上课，不联系")
            return result

        self.plugin.dialogue.decide = decide
        await scheduler.tick(wait=True)
        delivery = self.store.delivery(key)
        self.assertEqual(delivery["status"], "skipped")
        self.assertFalse(delivery["retryable"])
        self.assertEqual(delivery["skip_reason"], "schedule_cancelled")
        schedule.state.update(allowed=True, status="free")
        self.store.update_delivery(key, {"retry_at": time.time() - 1})
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertFalse(self.context.sent)

    async def test_busy_at_due_retry_cancels_without_another_model_call(self):
        scheduler, _target, schedule, key = self.setup_morning()
        await scheduler.tick(wait=True)
        self.assertTrue(self.store.delivery(key)["retryable"])
        self.store.update_delivery(key, {"retry_at": time.time() - 1})
        schedule.state.update(allowed=False, status="busy", reason="正在上课，不联系")
        await scheduler.tick(wait=True)
        self.assertFalse(self.store.delivery(key)["retryable"])
        schedule.state.update(allowed=True, status="free")
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertFalse(self.context.sent)


if __name__ == "__main__":
    unittest.main()
