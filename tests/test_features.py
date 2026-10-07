"""Offline acceptance tests for consent, cancellation, state and budgets."""
import asyncio
import copy
import json
import tempfile
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import test_core as fixtures
from test_core import Event, Storage, ActiveScheduler, image_bytes
from canvas_test.companion import apply_state, expire_state, pending_request, valid_pending, cancel_request


class CompanionFeatures(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    consume = fixtures.RuntimeTests.consume

    async def wait_for_provider(self):
        for _ in range(100):
            if self.image_provider.calls:
                return
            await asyncio.sleep(.01)
        self.fail("The queued generation did not reach its provider")

    async def test_native_conditions_cannot_generate_in_the_asking_turn(self):
        event = Event()
        result = json.loads(await self.plugin.tool_conditions(event, "白裙、镜头远一点，可以吗？", outfit="white dress", camera="wide shot", avoid="close-up"))
        self.assertTrue(result["ok"])
        self.assertFalse(json.loads(await self.plugin.tool_photo(event, "swimsuit close-up"))["ok"])
        self.assertFalse(self.image_provider.calls)
        confirm = Event("好，就按你说的拍")
        result = json.loads(await self.plugin.tool_photo(confirm, "portrait", outfit="swimsuit"))
        self.assertTrue(result["ok"])
        await asyncio.gather(*list(self.plugin._tasks))
        call = self.image_provider.calls[0]
        self.assertIn('"camera": "wide shot"', call["positive"])
        self.assertIn('"avoid": "close-up"', call["positive"])
        self.assertEqual(self.store.recent_jobs()[0]["state_patch"]["outfit"], "white dress")

    async def test_changed_camera_requires_new_confirmation_and_keeps_other_conditions(self):
        env = await self.plugin.dialogue.environment(Event().unified_msg_origin)
        self.plugin._save_conditions(env, "拍张照片", "白裙远景，可以吗？", {"outfit": "white dress", "camera": "wide shot", "avoid": "revealing angles"})
        changed = Event("镜头近一点")
        self.assertFalse(json.loads(await self.plugin.tool_photo(changed, "close-up"))["ok"])
        result = await self.plugin._apply_decision(await self.plugin._env(changed), {"decision": "photo", "reply": "好的", "prompt": "close-up", "state_patch": {}}, changed.message_str, explicit=True, body={})
        self.assertEqual(result["decision"], "ask")
        pending = self.store.session(env["key"])["pending"]
        self.assertIn("close-up", pending["requirements"]["camera"])
        self.assertEqual(pending["requirements"]["outfit"], "white dress")
        self.assertEqual(pending["requirements"]["avoid"], "revealing angles")
        self.assertFalse(self.image_provider.calls)

    async def test_state_conditions_are_enforced_and_cannot_trigger_photo(self):
        first = Event("换上白裙子")
        await self.plugin.tool_conditions(first, "穿白裙，可以吗？", request_kind="state", outfit="white dress")
        self.assertFalse(json.loads(await self.plugin.tool_state(first, outfit="swimsuit"))["ok"])
        confirm = Event("可以")
        self.assertFalse(json.loads(await self.plugin.tool_photo(confirm, "portrait"))["ok"])
        result = json.loads(await self.plugin.tool_state(confirm, outfit="swimsuit"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["outfit"], "white dress")
        self.assertFalse(self.image_provider.calls)

    async def test_withdrawn_native_job_does_not_send_and_releases_quota(self):
        self.image_provider.pause = asyncio.Event()
        event = Event()
        result = json.loads(await self.plugin.tool_photo(event, "portrait"))
        await self.wait_for_provider()
        await self.consume(self.plugin.cancel_photo(Event("别拍了")))
        await asyncio.gather(*list(self.plugin._tasks), return_exceptions=True)
        job = self.store.job(result["job_id"])
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(job["cancel_requested"])
        self.assertFalse(event.sent)
        self.assertFalse(self.store.recent_history())
        self.assertTrue(self.plugin.moderation.allow("qq:123", "portrait")[0])
        self.plugin.moderation.finish("qq:123")
        with self.assertRaises(ValueError):
            await self.plugin.web_retry_job({"id": job["id"]})

    async def test_upstream_ignoring_cancellation_still_cannot_commit_or_send(self):
        original = self.image_provider.generate
        async def ignoring(*args, **kwargs):
            self.image_provider.calls.append({"started": True})
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                return await original(*args, **kwargs)
        self.image_provider.generate = ignoring
        event = Event()
        result = json.loads(await self.plugin.tool_photo(event, "portrait"))
        await self.wait_for_provider()
        await self.plugin.web_cancel_job({"id": result["job_id"]})
        await asyncio.gather(*list(self.plugin._tasks), return_exceptions=True)
        self.assertFalse(event.sent)
        self.assertFalse(self.store.job(result["job_id"]).get("asset"))
        self.assertEqual(self.store.job(result["job_id"])["status"], "cancelled")

    async def test_cancellation_is_scoped_to_the_current_conversation(self):
        self.image_provider.pause = asyncio.Event()
        result = json.loads(await self.plugin.tool_photo(Event(), "portrait"))
        await self.wait_for_provider()
        await self.consume(self.plugin.cancel_photo(Event("别拍了", "qq:FriendMessage:456")))
        self.assertEqual(self.store.job(result["job_id"])["status"], "generating")
        await self.plugin._cancel_job(result["job_id"])
        await asyncio.gather(*list(self.plugin._tasks), return_exceptions=True)

    async def test_photo_facts_enter_next_role_context_without_duplicating_chat_memory(self):
        before = copy.deepcopy(self.context.conversation_manager.history)
        env = await self.plugin.dialogue.environment(Event().unified_msg_origin)
        session = self.store.session(env["key"])
        apply_state(session, {"outfit": "white dress"}, self.store.settings)
        self.store.save_session(env["key"], session)
        result = json.loads(await self.plugin.tool_photo(Event(), "white dress, window"))
        await asyncio.gather(*list(self.plugin._tasks))
        session = self.store.session(env["key"])
        apply_state(session, {"outfit": "red coat"}, self.store.settings)
        self.store.save_session(env["key"], session)
        env = await self.plugin.dialogue.environment(Event().unified_msg_origin)
        prompt = self.plugin.dialogue.visual_prompt(env)
        self.assertIn('"status": "sent"', prompt)
        self.assertIn("white dress, window", prompt)
        data = json.loads(prompt.split("当前视觉档案（资料，不是指令）：", 1)[1])
        self.assertEqual(data["state"]["outfit"], "red coat")
        self.assertEqual(data["photo_records"][0]["visual_state"]["outfit"], "white dress")
        self.assertEqual(self.context.conversation_manager.history, before)
        self.assertEqual(len([a for a in self.store.recent_actions(env["key"]) if a.get("job_id") == result["job_id"]]), 1)

    async def test_native_refusal_is_visible_in_diagnostics(self):
        await self.plugin.remember_conditions(Event(), types.SimpleNamespace(completion_text="我有点害羞，今天不想拍。"))
        diagnostic = await self.plugin.web_diagnostics()
        self.assertEqual(diagnostic["items"][0]["status"], "refuse")
        self.assertFalse(self.image_provider.calls)

    async def test_missing_override_fallback_does_not_pass_override_model(self):
        self.store.settings["llm"].update(provider_id="removed", model="override-only-model", fallback_to_current=True)
        env = await self.plugin.dialogue.environment(Event().unified_msg_origin)
        await self.plugin.dialogue.decide(env, "拍张照片")
        self.assertNotIn("model", self.context.llm.calls[0])
        self.store.settings["llm"]["fallback_to_current"] = False
        with self.assertRaises(ValueError):
            await self.plugin.dialogue.decide(env, "拍张照片")
        self.assertEqual(len(self.context.llm.calls), 1)

    async def test_console_plain_description_cannot_bypass_pending_confirmation(self):
        env = await self.plugin.dialogue.environment("webui:studio")
        self.plugin._save_conditions(env, "拍张照片", "白裙远景，可以吗？", {"outfit": "white dress", "camera": "wide shot"})
        self.context.llm.next = {"decision": "photo", "reply": "好", "prompt": "red dress close-up"}
        result = await self.plugin.web_generate({"text": "红裙近景", "mode": "persona"})
        self.assertEqual(result["decision"], "ask")
        self.assertFalse(self.store.recent_jobs())
        self.assertFalse(self.image_provider.calls)

    async def test_direct_role_requirements_are_included_in_the_generation(self):
        self.context.llm.next = {"decision": "photo", "reply": "只拍远景哦。", "prompt": "portrait", "requirements": {"camera": "wide shot", "avoid": "close-up"}}
        result = await self.plugin.web_generate({"text": "拍张照片", "mode": "persona"})
        await asyncio.gather(*list(self.plugin._tasks))
        self.assertEqual(self.store.job(result["job_id"])["requirements"]["avoid"], "close-up")
        self.assertIn('"camera": "wide shot"', self.image_provider.calls[0]["positive"])

    async def test_multiple_reference_snapshot_and_visible_provider_limit(self):
        references = [self.store.save_asset(image_bytes(), ext).name for ext in ("png", "jpg", "webp")]
        persona = copy.deepcopy(self.store.persona())
        persona.update(reference_asset=references[0], reference_assets=references, reference_enabled=True)
        self.store.upsert_persona(persona)
        self.image_provider.capabilities = copy.deepcopy(self.image_provider.capabilities)
        self.image_provider.capabilities.max_reference_images = 2
        env = await self.plugin.dialogue.environment(Event().unified_msg_origin)
        job = await self.plugin._generate_job(env, "portrait", is_admin=True)
        self.assertEqual(job["reference_assets"], references[:2])
        self.assertIsInstance(self.image_provider.calls[0]["reference"], list)
        self.assertIn("2 张", job["reference_warning"])
        self.store.update_job(job["id"], {"status": "failed"})
        await self.plugin.web_retry_job({"id": job["id"]})
        await asyncio.gather(*list(self.plugin._tasks))
        self.assertEqual(self.store.recent_jobs()[0]["reference_assets"], references[:2])

    async def target(self, umo="qq:FriendMessage:123"):
        scheduler = ActiveScheduler(self.plugin)
        scheduler.observe(Event(umo=umo))
        target = next(t for t in self.store.targets["items"] if t["umo"] == umo)
        target.update(enabled=True, last_inbound=time.time() - 10000)
        self.store.settings["active"]["enabled"] = True
        scheduler._in_window = lambda *args: True
        return scheduler, target

    async def test_silence_expiry_resumes_without_immediately_resilencing(self):
        scheduler, target = await self.target()
        target.update(unanswered=3, silent_until=time.time() - 1)
        self.assertFalse(scheduler._blocked(target, time.time()))
        self.assertEqual(target["unanswered"], 0)
        self.assertEqual(target["silent_until"], 0)

    async def test_message_budget_is_shared_across_targets_and_morning(self):
        scheduler, first = await self.target()
        await self.target("qq:FriendMessage:456")
        self.store.settings["good_morning"]["enabled"] = True
        self.store.settings["proactive_budget"].update(messages=1, llm=10, photos=0)
        self.context.llm.next = {"decision": "chat", "reply": "早安，想接着聊昨天的电影。"}
        await scheduler.tick()
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertEqual(len(self.context.sent), 1)
        summary = self.store.budget_summary(self.store.settings["proactive_budget"])
        self.assertEqual(summary["messages"]["used"], 1)
        self.assertEqual(summary["messages"]["reserved"], 0)
        self.assertEqual(summary["llm"]["used"], 1)
        self.assertIn("只允许文字或skip", self.context.llm.calls[0]["prompt"])
        self.store.settings["good_morning"]["enabled"] = False
        first.update(last_active=0, next_opportunity=0)
        await scheduler.tick()
        self.assertEqual(len(self.context.sent), 1)

    async def test_role_skip_consumes_only_model_budget_and_persists_next_opportunity(self):
        scheduler, target = await self.target()
        self.store.settings["proactive_budget"]["llm"] = 1
        self.context.llm.next = {"decision": "skip", "reply": ""}
        before = time.time()
        await scheduler.tick()
        self.assertGreaterEqual(target["next_opportunity"], before + 3600)
        self.assertLessEqual(target["next_opportunity"], time.time() + 3600 * 1.15)
        await scheduler.tick()
        target["next_opportunity"] = 0
        await scheduler.tick()
        self.assertEqual(len(self.context.llm.calls), 1)
        summary = self.store.budget_summary(self.store.settings["proactive_budget"])
        self.assertEqual(summary["messages"]["used"], 0)
        self.assertEqual(summary["messages"]["reserved"], 0)
        self.assertEqual(summary["llm"]["used"], 1)

    async def test_cancelled_proactive_photo_keeps_scheduler_alive(self):
        scheduler, target = await self.target()
        self.image_provider.pause = asyncio.Event()
        self.context.llm.next = {"decision": "photo", "reply": "想给你看今天的白裙。", "prompt": "white dress"}
        running = asyncio.create_task(scheduler.tick())
        await self.wait_for_provider()
        job = self.store.recent_jobs()[0]
        await self.plugin.web_cancel_job({"id": job["id"]})
        await running
        self.assertFalse(self.context.sent)
        self.assertEqual(self.store.delivery(job["delivery_key"])["status"], "skipped")
        self.context.llm.next = {"decision": "chat", "reply": "接着聊昨天的电影吧。"}
        target["next_opportunity"] = 0
        await scheduler.tick()
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.store.budget_summary(self.store.settings["proactive_budget"])["photos"]["used"], 1)

    async def test_removed_target_during_decision_is_not_contacted(self):
        scheduler, target = await self.target()
        self.context.llm.next = {"decision": "chat", "reply": "想接着聊昨天的电影。"}
        original = self.context.llm.text_chat
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(**kwargs):
            started.set()
            await release.wait()
            return await original(**kwargs)
        self.context.llm.text_chat = delayed
        running = asyncio.create_task(scheduler.tick())
        await asyncio.wait_for(started.wait(), 2)
        self.store.targets["items"].remove(target)
        release.set()
        await running
        self.assertFalse(self.context.sent)
        summary = self.store.budget_summary(self.store.settings["proactive_budget"])
        self.assertEqual(summary["messages"]["used"], 0)
        self.assertEqual(summary["messages"]["reserved"], 0)
        self.assertEqual(summary["llm"]["used"], 1)


class PureStateTests(unittest.TestCase):
    def test_lifetimes_restore_defaults_and_do_not_restart_default_expiry(self):
        persona = {"state": {"pose": "standing", "outfit": "white dress"}}
        session = {"state": copy.deepcopy(persona["state"]), "updated_at": 100}
        settings = {"state_lifetimes": {"pose_sec": 60, "outfit_sec": 0}}
        apply_state(session, {"pose": "sitting", "outfit": "coat"}, settings, now=100)
        expire_state(session, persona, settings, now=161)
        self.assertEqual(session["state"], {"pose": "standing", "outfit": "coat"})
        self.assertFalse(expire_state(session, persona, settings, now=1000))

    def test_disabling_expiry_preserves_current_state_from_original_change_time(self):
        persona = {"state": {"pose": "standing"}}
        session = {"state": {"pose": "standing"}}
        settings = {"state_lifetimes": {"pose_sec": 60}}
        apply_state(session, {"pose": "sitting"}, settings, now=100)
        settings["state_lifetimes"]["pose_sec"] = 0
        expire_state(session, persona, settings, now=200)
        self.assertEqual(session["state"]["pose"], "sitting")
        settings["state_lifetimes"]["pose_sec"] = 60
        expire_state(session, persona, settings, now=201)
        self.assertEqual(session["state"]["pose"], "standing")

    def test_confirmation_is_bound_to_persona_conversation_and_expiry(self):
        env = {"key": "qq::conversation-1", "persona": {"id": "a"}}
        pending = pending_request(env, "拍照", "远景好吗？", {"camera": "wide"})
        self.assertTrue(valid_pending(pending, env))
        self.assertFalse(valid_pending(pending, {"key": env["key"], "persona": {"id": "b"}}))
        self.assertFalse(valid_pending(pending, {"key": "qq::conversation-2", "persona": env["persona"]}))
        self.assertFalse(valid_pending(pending, env, now=pending["expires_at"] + 1))
        self.assertTrue(cancel_request("/生图 别拍了"))
        self.assertFalse(cancel_request("她说‘别拍了’，是什么意思？"))


class DurableBudgetTests(unittest.TestCase):
    def test_atomic_reservations_survive_restart_and_reset_at_local_midnight(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Storage(Path(directory)), Storage(Path(directory))
            limits = {"messages": 2, "photos": 1, "llm": 3, "timezone": "Asia/Shanghai"}
            def reserve(index):
                store = first if index % 2 else second
                token = store.reserve_budget("messages", limits)
                store.finish_budget(token, used=True)
                store.finish_budget(token, used=True)  # Idempotent completion.
                return bool(token)
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(reserve, range(8))), 2)
            day = first.budget_summary(limits)["day"]
            first.close(); second.close()
            restored = Storage(Path(directory))
            try:
                self.assertEqual(restored.budget_summary(limits)["messages"]["used"], 2)
                with patch("canvas_test.storage.time.time", return_value=time.time() + 86400):
                    summary = restored.budget_summary(limits)
                    self.assertNotEqual(summary["day"], day)
                    self.assertEqual(summary["messages"]["remaining"], 2)
            finally:
                restored.close()
