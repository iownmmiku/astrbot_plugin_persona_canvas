"""Offline regressions for independent proactive work and stale fallback memory."""
import asyncio
import copy
import time
import unittest

import test_core as fixtures
from test_core import ActiveScheduler, Event


class Runtime056Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    def scheduler(self, count=2):
        scheduler = ActiveScheduler(self.plugin)
        self.plugin.active = scheduler
        scheduler._in_window = lambda *args: True
        self.store.settings["active"].update(enabled=True, max_concurrent_targets=2, min_idle_sec=0, min_gap_sec=60, default_start="00:00", default_end="23:59")
        self.store.settings["good_morning"]["enabled"] = False
        self.store.settings["proactive_budget"].update(messages=20, photos=20, llm=20)
        for index in range(count):
            scheduler.observe(Event(umo=f"qq:FriendMessage:{index}"))
        for target in self.store.targets["items"]:
            target.update(enabled=True, last_inbound=0, last_active=0)
        self.store.save_targets()
        return scheduler

    async def until(self, condition):
        async with asyncio.timeout(2):
            while not condition():
                await asyncio.sleep(.001)

    async def settle(self, scheduler):
        while scheduler._target_tasks:
            await asyncio.gather(*list(scheduler._target_tasks.values()), return_exceptions=True)
            await asyncio.sleep(0)

    @staticmethod
    def decision(reply, photo=False):
        return {"decision": "photo" if photo else "chat", "reply": reply, "prompt": "blue dress" if photo else "", "state_patch": {}}

    async def test_slow_target_does_not_hold_other_targets_or_the_next_tick(self):
        scheduler = self.scheduler(3)
        first_release = asyncio.Event()
        entered = []
        async def decide(env, *_args, **_kwargs):
            entered.append(env["umo"])
            if env["umo"].endswith(":0"):
                await first_release.wait()
            return self.decision("接着聊昨天的话题。")
        self.plugin.dialogue.decide = decide
        await asyncio.wait_for(scheduler.tick(), .5)
        await self.until(lambda: len(self.context.sent) == 1 and len(scheduler._target_tasks) == 1)
        self.assertEqual(self.context.sent[0][0], "qq:FriendMessage:1")
        self.assertIn("qq:FriendMessage:0", scheduler._target_tasks)
        await asyncio.wait_for(scheduler.tick(), .5)
        await self.until(lambda: len(self.context.sent) == 2)
        self.assertEqual(self.context.sent[1][0], "qq:FriendMessage:2")
        self.assertEqual(len(entered), 3)
        first_release.set()
        await self.settle(scheduler)
        self.assertEqual(len(self.context.sent), 3)

    async def test_overlapping_ticks_cannot_duplicate_targets_or_accumulate_waiters(self):
        scheduler = self.scheduler(8)
        release = asyncio.Event()
        entered = []
        async def decide(env, *_args, **_kwargs):
            entered.append(env["umo"])
            await release.wait()
            return self.decision("今天过得怎么样？")
        self.plugin.dialogue.decide = decide
        await asyncio.gather(*(scheduler.tick() for _ in range(12)))
        await self.until(lambda: len(entered) == 2)
        self.assertEqual(len(set(entered)), 2)
        self.assertEqual(len(scheduler._target_tasks), 2)
        self.assertEqual(len([task for task in self.plugin._tasks if task.get_name() == "persona-canvas-active-attempt"]), 2)
        release.set()
        await self.settle(scheduler)
        await scheduler.tick(wait=True)
        self.assertEqual(len(set(entered)), 4)
        self.assertEqual(set(entered), {f"qq:FriendMessage:{index}" for index in range(4)})
        self.assertEqual(len(self.context.sent), 4)

    async def test_waiting_one_shot_tick_releases_the_dispatch_lock(self):
        scheduler = self.scheduler()
        release = asyncio.Event()
        entered = []
        async def decide(env, *_args, **_kwargs):
            entered.append(env["umo"])
            await release.wait()
            return self.decision("想接着聊电影。")
        self.plugin.dialogue.decide = decide
        first = asyncio.create_task(scheduler.tick(wait=True))
        await self.until(lambda: len(entered) == 2)
        await asyncio.wait_for(scheduler.tick(), .5)
        self.assertFalse(first.done())
        self.assertFalse(scheduler._tick_lock.locked())
        release.set()
        await first

    async def test_cancelling_one_proactive_photo_does_not_stop_another_target(self):
        scheduler = self.scheduler()
        self.image_provider.pause = asyncio.Event()
        self.plugin.dialogue.decide = lambda *_args, **_kwargs: self.photo_decision()
        await scheduler.tick()
        await self.until(lambda: len(self.image_provider.calls) == 2)
        jobs = sorted(self.store.recent_jobs(), key=lambda job: job["umo"])
        await self.plugin.web_cancel_job({"id": jobs[0]["id"]})
        await self.until(lambda: self.store.delivery(jobs[0]["delivery_key"])["status"] == "skipped")
        self.image_provider.pause.set()
        await self.settle(scheduler)
        self.assertEqual(self.store.job(jobs[0]["id"])["status"], "cancelled")
        self.assertEqual(self.store.job(jobs[1]["id"])["status"], "sent")
        self.assertEqual([umo for umo, _chain in self.context.sent], [jobs[1]["umo"]])

    async def photo_decision(self):
        return self.decision("想给你看今天的衣服。", photo=True)

    async def test_proactive_parallelism_still_obeys_global_generation_slots(self):
        scheduler = self.scheduler()
        self.store.settings["generation"]["max_concurrency"] = 1
        self.image_provider.pause = asyncio.Event()
        self.plugin.dialogue.decide = lambda *_args, **_kwargs: self.photo_decision()
        await scheduler.tick()
        await self.until(lambda: len(self.store.recent_jobs()) == 2 and len(self.image_provider.calls) == 1)
        self.assertEqual(self.plugin._running, 1)
        self.image_provider.pause.set()
        await self.settle(scheduler)
        self.assertEqual(len(self.image_provider.calls), 2)
        self.assertEqual(len(self.context.sent), 2)
        self.assertEqual(self.plugin._running, 0)

    async def test_plugin_stop_cancels_all_dispatched_targets_before_closing_storage(self):
        scheduler = self.scheduler(4)
        release = asyncio.Event()
        async def decide(*_args, **_kwargs):
            await release.wait()
            return self.decision("晚安。")
        self.plugin.dialogue.decide = decide
        await scheduler.tick()
        await self.until(lambda: self.store.budget_summary(self.store.settings["proactive_budget"])["messages"]["reserved"] == 2)
        tasks = list(scheduler._target_tasks.values())
        await self.plugin.terminate()
        self.assertTrue(all(task.done() for task in tasks))
        self.assertFalse(self.context.sent)
        self.assertTrue(self.store._closed)
        reopened = fixtures.Storage(self.store.root)
        try:
            self.assertEqual(reopened.budget_summary(reopened.settings["proactive_budget"])["messages"]["reserved"], 0)
        finally:
            reopened.close()

    def morning_scheduler(self):
        scheduler = self.scheduler(1)
        self.store.settings["active"]["enabled"] = False
        self.store.settings["good_morning"].update(enabled=True, start="00:00", end="23:59")
        return scheduler

    async def assert_morning_photo_is_not_redrawn(self, scheduler, status):
        await scheduler.tick(wait=True)
        job, = self.store.recent_jobs()
        delivery = self.store.delivery(job["delivery_key"])
        self.assertEqual(job["status"], status)
        self.assertEqual(delivery["status"], status)
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.store.update_delivery(delivery["key"], {"retry_at": 0})
        await scheduler.tick(wait=True)
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(len(self.store.recent_jobs()), 1)
        self.assertEqual(self.store.delivery(delivery["key"])["attempts"], 1)

    async def test_rejected_proactive_photo_retains_image_and_never_automatically_redraws(self):
        scheduler = self.morning_scheduler()
        async def reject(*_args):
            return False
        self.context.send_message = reject
        self.plugin.dialogue.decide = lambda *_args, **_kwargs: self.photo_decision()
        await self.assert_morning_photo_is_not_redrawn(scheduler, "delivery_failed")

    async def test_unknown_proactive_send_outcome_is_preserved_and_not_retried(self):
        scheduler = self.morning_scheduler()
        async def disconnect(*_args):
            raise ConnectionError("模拟发送回执丢失")
        self.context.send_message = disconnect
        self.plugin.dialogue.decide = lambda *_args, **_kwargs: self.photo_decision()
        await self.assert_morning_photo_is_not_redrawn(scheduler, "uncertain")

    async def test_stopped_proactive_send_remains_uncertain_and_is_not_redrawn(self):
        scheduler = self.morning_scheduler()
        sending = asyncio.Event()
        async def paused_send(*_args):
            sending.set()
            await asyncio.Event().wait()
        self.context.send_message = paused_send
        self.plugin.dialogue.decide = lambda *_args, **_kwargs: self.photo_decision()
        await scheduler.tick()
        await asyncio.wait_for(sending.wait(), 2)
        task, = scheduler._target_tasks.values()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.assert_morning_photo_is_not_redrawn(scheduler, "uncertain")

    async def test_rejected_proactive_text_has_a_definite_delivery_failure(self):
        scheduler = self.morning_scheduler()
        calls = []
        async def decide(*_args, **_kwargs):
            calls.append(True)
            return self.decision("早上好。")
        async def reject(*_args):
            return False
        self.plugin.dialogue.decide = decide
        self.context.send_message = reject
        await scheduler.tick(wait=True)
        key, = self.store._db.execute("SELECT key FROM deliveries").fetchone()
        delivery = self.store.delivery(key)
        self.assertEqual(delivery["status"], "delivery_failed")
        self.store.update_delivery(delivery["key"], {"retry_at": 0})
        await scheduler.tick(wait=True)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.image_provider.calls)

    async def test_removed_target_is_not_contacted_after_an_inflight_role_decision(self):
        scheduler = self.scheduler(1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def decide(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return self.decision("想继续聊昨晚的话题。")
        self.plugin.dialogue.decide = decide
        await scheduler.tick()
        await asyncio.wait_for(entered.wait(), 2)
        self.store.targets["items"].clear()
        self.store.save_targets()
        release.set()
        await self.settle(scheduler)
        key, = self.store._db.execute("SELECT key FROM deliveries").fetchone()
        delivery = self.store.delivery(key)
        self.assertEqual(delivery["status"], "skipped")
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.budget_summary(self.store.settings["proactive_budget"])["messages"]["reserved"], 0)

    async def fallback_environment(self):
        self.context.conversation_manager.cid = ""
        return await self.plugin.dialogue.environment("qq:FriendMessage:memory")

    async def test_fallback_memory_cannot_switch_back_to_the_old_persona(self):
        env = await self.fallback_environment()
        self.store.upsert_persona({"id": "other", "name": "另一个角色"})
        switched = self.store.session(env["key"], "other")
        switched["pending"] = {"request_id": "new-role-conditions", "expires_at": time.time() + 100}
        switched = self.store.save_session(env["key"], switched)
        await self.plugin.dialogue.remember(env, "旧请求", "旧角色的回复")
        self.assertEqual(self.store.read_session(env["key"]), switched)

    async def test_fallback_memory_skips_newer_conditions_in_the_same_persona(self):
        env = await self.fallback_environment()
        current = self.store.read_session(env["key"])
        current["pending"] = {"request_id": "new-conditions", "expires_at": time.time() + 100}
        current = self.store.save_session(env["key"], current)
        await self.plugin.dialogue.remember(env, "旧请求", "过期回复")
        self.assertEqual(self.store.read_session(env["key"]), current)

    async def test_fallback_memory_does_not_recreate_a_deleted_session(self):
        env = await self.fallback_environment()
        with self.store._db:
            self.store._db.execute("DELETE FROM sessions WHERE umo=?", (env["key"],))
        await self.plugin.dialogue.remember(env, "旧请求", "过期回复")
        self.assertIsNone(self.store.read_session(env["key"]))

    async def test_current_fallback_memory_updates_context_and_its_revision(self):
        env = await self.fallback_environment()
        initial = copy.deepcopy(env["session"])
        await self.plugin.dialogue.remember(env, "你好", "今天怎么样？")
        await self.plugin.dialogue.remember(env, "不错", "那就好。")
        current = self.store.read_session(env["key"])
        self.assertEqual([item["content"] for item in current["last_context"]], ["你好", "今天怎么样？", "不错", "那就好。"])
        self.assertEqual(current["revision"], initial["revision"] + 2)
        self.assertEqual(env["session"], current)
