"""Offline workflow regressions: retries, agreement edits and saved-image resend."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock

import test_core as fixtures
import test_tool_failures as group_fixtures
from test_core import Event
from canvas_test.page_api import dispatch_api
from canvas_test.providers.base import provider_from_config


class Release056Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    seed_pending = group_fixtures.GroupConfirmationTests.seed_pending

    async def settle(self):
        while self.plugin._tasks:
            await asyncio.gather(*list(self.plugin._tasks), return_exceptions=True)

    async def saved_job(self, status="delivery_failed"):
        env = await self.plugin.dialogue.environment(Event().unified_msg_origin)
        job = await self.plugin._generate_job(env, "portrait", raw="拍一张自拍", is_admin=True)
        return self.store.update_job(job["id"], {"status": status})

    async def test_unconditional_retry_double_click_calls_provider_once(self):
        self.image_provider.error = "offline failure"
        first = json.loads(await self.plugin.tool_photo(Event("拍一张自拍"), "portrait"))
        await self.settle()
        self.image_provider.error = None
        self.image_provider.pause = asyncio.Event()
        results = await asyncio.gather(*(self.plugin.web_retry_job({"id": first["job_id"]}) for _ in range(2)))
        self.assertEqual(results[0]["job_id"], results[1]["job_id"])
        for _ in range(100):
            if len(self.image_provider.calls) == 2:
                break
            await asyncio.sleep(.01)
        self.assertEqual(len(self.image_provider.calls), 2)
        self.image_provider.pause.set()
        await self.settle()
        again = await self.plugin.web_retry_job({"id": first["job_id"]})
        self.assertEqual(again["job_id"], results[0]["job_id"])
        self.assertEqual(len(self.image_provider.calls), 2)

    async def test_explicit_platform_rejection_preserves_image_for_resend(self):
        job = await self.saved_job("succeeded")
        event = Event()
        event.send = AsyncMock(return_value=False)
        with self.assertRaises(fixtures.main.DeliveryRejectedError):
            await self.plugin._send_job(job, event=event)
        rejected = self.store.job(job["id"])
        self.assertEqual(rejected["status"], "delivery_failed")
        self.assertEqual(rejected["asset"], job["asset"])
        self.assertEqual(rejected["failure_stage"], "delivery")
        with self.assertRaises(ValueError):
            await self.plugin.web_retry_job({"id": job["id"]})

    async def test_concurrent_resends_send_original_once_without_generation(self):
        job = await self.saved_job()
        self.context.send_message = AsyncMock(return_value=True)
        results = await asyncio.gather(*(dispatch_api(self.plugin, "POST", "jobs/resend", {"id": job["id"]}) for _ in range(2)))
        self.assertTrue(all(r["ok"] for r in results))
        self.context.send_message.assert_awaited_once()
        destination, chain = self.context.send_message.call_args.args
        self.assertEqual(destination, job["umo"])
        self.assertIn(("image", str(self.store.asset(job["asset"]))), chain.chain)
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(self.store.job(job["id"])["resend_count"], 1)
        self.assertEqual((await self.plugin.web_history())["items"][0]["status"], "sent")

    async def test_unknown_ack_requires_explicit_check_and_retains_unknown_on_error(self):
        job = await self.saved_job("succeeded")
        self.context.send_message = AsyncMock(side_effect=TimeoutError("offline ack timeout"))
        with self.assertRaises(TimeoutError):
            await self.plugin._send_job(job, umo=job["umo"])
        self.assertEqual(self.store.job(job["id"])["status"], "uncertain")
        with self.assertRaisesRegex(ValueError, "检查聊天记录"):
            await self.plugin.web_resend_job({"id": job["id"]})
        self.context.send_message = AsyncMock(return_value=True)
        sent = await self.plugin.web_resend_job({"id": job["id"], "confirm_uncertain": True})
        self.assertTrue(sent["ok"])
        self.assertFalse(sent["generated"])
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_concurrent_resend_does_not_send_twice_after_unknown_or_rejected_ack(self):
        for outcome in (False, TimeoutError("offline ack lost")):
            with self.subTest(outcome=outcome):
                job = await self.saved_job("uncertain")
                started, release = asyncio.Event(), asyncio.Event()
                calls = []
                async def delayed(*args):
                    calls.append(args)
                    started.set()
                    await release.wait()
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                self.context.send_message = delayed
                body = {"id": job["id"], "confirm_uncertain": True}
                first = asyncio.create_task(self.plugin.web_resend_job(body))
                await asyncio.wait_for(started.wait(), 2)
                with self.assertRaisesRegex(ValueError, "正在发送"):
                    await self.plugin.web_resend_job(body)
                release.set()
                result = await first
                self.assertFalse(result["ok"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.store.job(job["id"])["resend_count"], 1)

    async def test_resend_tool_scoped_to_conversation_and_semantic_request(self):
        job = await self.saved_job()
        other = Event("刚才那张重新发一下", "qq:FriendMessage:456")
        denied = json.loads(await self.plugin.tool_control(other, "resend", "用户要求补发原图", job_id=job["id"]))
        self.assertFalse(denied["ok"])
        self.assertFalse(self.context.sent)
        event = Event("不用重新生成，把刚才那张发我")
        result = json.loads(await self.plugin.tool_control(event, "resend", "用户明确要求原图补发而非重新绘制", job_id=job["id"]))
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.context.sent[0][0], job["umo"])
        self.assertFalse(event.sent)
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_resend_refuses_cancelled_changed_persona_and_studio_destination(self):
        for patch in ({"cancel_requested": True}, {"umo": "webui:studio"}, {"status": "sending"}):
            with self.subTest(patch=patch):
                job = await self.saved_job()
                self.store.update_job(job["id"], patch)
                with self.assertRaises(ValueError):
                    await self.plugin.web_resend_job({"id": job["id"]})
        job = await self.saved_job()
        self.store.upsert_persona({"id": "other", "name": "另一角色"})
        self.store.session(job["session_key"], "other")
        with self.assertRaisesRegex(ValueError, "角色已改变"):
            await self.plugin.web_resend_job({"id": job["id"]})
        self.assertFalse(self.context.sent)

    async def test_revise_can_delete_constraints_without_resurrecting_old_notes(self):
        event = Event("改一下，不限制服装和特写了")
        key, pending = await self.seed_pending(event)
        changed = json.loads(await self.plugin.tool_control(event, "revise", "双方调整约定，移除服装和避免条件", request_id=pending["request_id"], reply="现在只保留远景，可以吗？", updates={"outfit": "", "avoid": None}, remove_fields=["pose"]))
        self.assertTrue(changed["ok"], changed)
        current = self.store.read_session(key)["pending"]
        self.assertNotEqual(current["request_id"], pending["request_id"])
        self.assertNotIn("outfit", current["requirements"])
        self.assertNotIn("avoid", current["requirements"])
        self.assertEqual(current["requirements"]["notes"], "现在只保留远景，可以吗？")
        blocked = json.loads(await self.plugin.tool_photo(event, "portrait", request_summary="希望拍照", confirmed_request_id=current["request_id"]))
        self.assertFalse(blocked["ok"])
        confirmation = Event("我同意现在只保留远景的条件")
        result = json.loads(await self.plugin.tool_photo(confirmation, "portrait", request_summary="用户接受刚更新的全部条件", confirmed_request_id=current["request_id"]))
        self.assertTrue(result["ok"], result)
        await self.settle()
        self.assertNotIn("close-up", self.image_provider.calls[0]["negative"])

    async def test_confirm_cannot_silently_delete_agreement(self):
        event = Event("照原样拍")
        _key, pending = await self.seed_pending(event)
        for kwargs in ({"updates": {"outfit": ""}}, {"remove_fields": ["avoid"]}):
            result = json.loads(await self.plugin.tool_control(event, "confirm", "接受原条件", request_id=pending["request_id"], **kwargs))
            self.assertFalse(result["ok"], result)
        self.assertFalse(self.image_provider.calls)

    async def test_conditions_notes_follow_new_role_agreement_instead_of_user_proposal(self):
        event = Event("那就近景吧")
        key, pending = await self.seed_pending(event)
        result = json.loads(await self.plugin.tool_conditions(event, "我仍只接受远景，可以吗？", camera="远景 / wide shot", request_summary="用户提议近景，角色只愿意远景并重新询问"))
        self.assertTrue(result["ok"], result)
        current = self.store.read_session(key)["pending"]
        self.assertNotEqual(current["request_id"], pending["request_id"])
        self.assertEqual(current["requirements"]["notes"], "我仍只接受远景，可以吗？")
        self.assertEqual(current["requirements"]["camera"], "远景 / wide shot")

    async def test_provider_mapping_preserve_disable_and_explicit_reset(self):
        config = {"name": "custom-test", "kind": "custom", "endpoint": "https://example.com", "model": "mock", "option_fields": {"steps": "parameters.steps"}}
        await self.plugin.web_save_provider(config)
        await self.plugin.web_save_provider({"name": "custom-test", "model": "mock2"})
        self.assertEqual(self.store.settings["providers"]["custom-test"]["option_fields"], config["option_fields"])
        await self.plugin.web_save_provider({"name": "custom-test", "option_fields": {}})
        self.assertEqual(self.store.settings["providers"]["custom-test"]["option_fields"], {})
        await self.plugin.web_save_provider({"name": "custom-test", "reset_option_fields": True})
        final = self.store.settings["providers"]["custom-test"]
        self.assertNotIn("option_fields", final)
        provider = provider_from_config("custom-test", final)
        self.assertTrue(provider.config.get("model"))

    async def test_target_concurrency_settings_validate_and_persist(self):
        await self.plugin.web_save_settings({"active": {"max_concurrent_targets": 2}})
        self.assertEqual(self.store.settings["active"]["max_concurrent_targets"], 2)
        with self.assertRaises(ValueError):
            await self.plugin.web_save_settings({"active": {"max_concurrent_targets": 17}})
