"""ComfyUI recovery through real plugin storage; all network/platform IO mocked."""
import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

import test_core as fixtures
from test_comfyui import PNG, ProviderError, provider, working_http
from canvas_test.providers.base import GeneratedImage
from canvas_test.storage import Storage


class RecoveryRuntime0516Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    async def saved_job(self, *, status="failed", prompt_id="task-123", cancelled=False):
        env = await self.plugin.dialogue.environment(fixtures.Event().unified_msg_origin)
        record = {"kind": "comfyui", "provider": "default", "prompt_id": prompt_id,
                  "fingerprint": "a" * 64, "phase": "accepted" if prompt_id else "submitting",
                  "submitted": True, "upstream_accepted": bool(prompt_id)}
        job = self.store.create_job({
            "status": status, "provider": "default", "source": "chat", "mode": "persona",
            "umo": env["umo"], "session_key": env["key"], "persona_id": env["persona"]["id"],
            "persona_snapshot": copy.deepcopy(env["persona"]), "state_patch": {"outfit": "old clothes"},
            "request_prompt": "original portrait", "caption": "原图", "recovery": record,
            "cancel_requested": cancelled,
        })
        return env, job

    def recover_mock(self, *, phase="completed", image=True):
        item = self.image_provider
        item.last_recovery = {"phase": phase, "prompt_id": "task-123"}
        item.recover = AsyncMock(return_value=GeneratedImage(PNG, "png", "default", "saved workflow") if image else None)
        return item.recover

    async def test_recovery_saves_only_original_image_without_send_or_visual_commit(self):
        env, job = await self.saved_job()
        session = self.store.read_session(env["key"])
        session["state"]["outfit"] = "new clothes"
        session["pending"] = {"request_id": "new-conditions", "requirements": {"outfit": "new clothes"}}
        self.store.save_session(env["key"], session)
        before = copy.deepcopy(self.store.read_session(env["key"]))
        history = copy.deepcopy(self.context.conversation_manager.history)
        recover = self.recover_mock()

        result = await self.plugin.web_recover_job({"id": job["id"]})

        recover.assert_awaited_once_with("task-123", fingerprint="a" * 64)
        self.assertEqual(result["recovery_status"], "completed")
        self.assertFalse(result["generated"])
        self.assertTrue(result["image_saved"])
        saved = self.store.job(job["id"])
        self.assertEqual(saved["status"], "succeeded")
        self.assertTrue(saved["recovered"])
        self.assertTrue(saved["delivery_blocked"])
        self.assertFalse(saved["state_committed"])
        self.assertEqual(self.store.asset(saved["asset"]).read_bytes(), PNG)
        self.assertEqual(self.store.read_session(env["key"]), before)
        self.assertEqual(self.context.conversation_manager.history, history)
        self.assertFalse(self.context.sent)
        self.assertFalse(self.image_provider.calls)
        with self.assertRaises(ValueError):
            await self.plugin.web_resend_job({"id": job["id"]})
        with self.assertRaisesRegex(fixtures.main.DeliveryRejectedError, "仅供查看"):
            await self.plugin._send_job(saved, umo=saved["umo"])
        self.assertFalse(self.context.sent)

    async def test_cancelled_task_can_be_viewed_but_remains_cancelled_and_unsendable(self):
        env, job = await self.saved_job(status="cancelled", cancelled=True)
        before = self.store.read_session(env["key"])
        self.recover_mock()
        result = await self.plugin.web_recover_job({"id": job["id"]})
        saved = self.store.job(job["id"])
        self.assertTrue(result["image_saved"])
        self.assertEqual(saved["status"], "cancelled")
        self.assertTrue(saved["cancel_requested"])
        self.assertTrue(saved["delivery_blocked"])
        self.assertEqual(self.store.read_session(env["key"]), before)
        with self.assertRaises(ValueError):
            await self.plugin.web_resend_job({"id": job["id"], "confirm_uncertain": True})
        self.assertFalse(self.context.sent)

    async def test_concurrent_recovery_reads_and_saves_original_once(self):
        _, job = await self.saved_job()
        started, release = asyncio.Event(), asyncio.Event()
        self.recover_mock()

        async def delayed(*_args, **_kwargs):
            started.set()
            await release.wait()
            return GeneratedImage(PNG, "png", "default", "saved workflow")

        self.image_provider.recover.side_effect = delayed
        first = asyncio.create_task(self.plugin.web_recover_job({"id": job["id"]}))
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(self.plugin.web_recover_job({"id": job["id"]}))
        release.set()
        results = await asyncio.gather(first, second)
        self.image_provider.recover.assert_awaited_once()
        self.assertEqual([r["recovery_status"] for r in results], ["completed", "completed"])
        self.assertTrue(results[1]["reused"])
        self.assertEqual(len(self.store.recent_history()), 1)
        self.assertFalse(self.context.sent)

    async def test_queue_and_missing_statuses_are_preserved_without_success_or_resubmission(self):
        for phase in ("queued", "running", "missing"):
            with self.subTest(phase=phase):
                _, job = await self.saved_job()
                recover = self.recover_mock(phase=phase, image=False)
                result = await self.plugin.web_recover_job({"id": job["id"]})
                recover.assert_awaited_once()
                self.assertFalse(result["image_saved"])
                self.assertFalse(result["generated"])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["recovery_status"], phase)
                self.assertEqual(self.store.job(job["id"])["recovery"]["phase"], phase)
                self.assertFalse(self.store.job(job["id"]).get("asset"))
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.context.sent)

    async def test_unreliable_number_and_still_running_local_job_reject_before_recovery(self):
        recover = self.recover_mock()
        _, unknown = await self.saved_job(prompt_id="")
        with self.assertRaisesRegex(ValueError, "可靠"):
            await self.plugin.web_recover_job({"id": unknown["id"]})
        for status in ("queued", "deciding", "generating", "sending"):
            _, job = await self.saved_job(status=status)
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "本地执行"):
                await self.plugin.web_recover_job({"id": job["id"]})
        recover.assert_not_awaited()
        self.assertFalse(self.context.sent)

    async def test_changed_workflow_fingerprint_rejects_before_remote_calls(self):
        _, job = await self.saved_job()
        item = provider()
        item._http = AsyncMock()
        self.plugin._provider = lambda _name=None: item
        with self.assertRaisesRegex(ProviderError, "已改变"):
            await self.plugin.web_recover_job({"id": job["id"]})
        item._http.assert_not_called()
        self.assertFalse(self.store.job(job["id"]).get("asset"))

    async def test_accepted_and_uncertain_submissions_cannot_retry_as_new_generation(self):
        for identifier in ("task-123", ""):
            _, job = await self.saved_job(prompt_id=identifier)
            with self.subTest(prompt_id=identifier), self.assertRaisesRegex(ValueError, "可能已受理"):
                await self.plugin.web_retry_job({"id": job["id"]})
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.context.sent)

    async def test_generation_persists_accepted_id_before_history_returns(self):
        env = await self.plugin.dialogue.environment(fixtures.Event().unified_msg_origin)
        item = provider()
        self.plugin._provider = lambda _name=None: item
        started, release = asyncio.Event(), asyncio.Event()
        original_http = working_http(item).side_effect

        async def delayed_http(method, url, body=None, **kwargs):
            if "/history/" in url:
                started.set()
                await release.wait()
            return await original_http(method, url, body, **kwargs)

        item._http.side_effect = delayed_http
        task = asyncio.create_task(self.plugin._generate_job(env, "portrait", source="chat", is_admin=True))
        try:
            await asyncio.wait_for(started.wait(), 2)
            job, = self.store.recent_jobs()
            self.assertEqual(job["status"], "generating")
            self.assertEqual(job["recovery"]["phase"], "accepted")
            self.assertEqual(job["recovery"]["prompt_id"], "task-123")
            self.assertTrue(job["recovery"]["upstream_accepted"])
            self.assertFalse(job.get("asset"))
            reopened = Storage(self.store.root)
            try:
                self.assertEqual(reopened.job(job["id"])["recovery"], job["recovery"])
            finally:
                reopened.close()
        finally:
            release.set()
            await task
        saved = self.store.job(job["id"])
        self.assertEqual(saved["status"], "succeeded")
        self.assertEqual(saved["recovery"]["phase"], "completed")
        self.assertFalse(self.context.sent)

    async def test_restart_retains_recovery_and_terminates_automatic_delivery_retry(self):
        for prompt_id in ("task-123", ""):
            _, job = await self.saved_job(status="generating", prompt_id=prompt_id)
            key = "morning:restart:" + (prompt_id or "unknown")
            self.store.reserve_delivery(key)
            self.store.update_delivery(key, {"kind": "morning", "status": "generating", "job_id": job["id"], "umo": job["umo"]})
            restarted_store = Storage(self.store.root)
            with patch.object(fixtures.main, "Storage", return_value=restarted_store):
                restarted = fixtures.main.PersonaCanvasPlugin(self.context, {"enable_webui": False})
            # Exercise initialization without launching periodic workers in this
            # test. All persisted state uses the real second SQLite connection.
            restarted._spawn = lambda coroutine, *_args: coroutine.close()
            try:
                await restarted.initialize()
                restored = restarted_store.job(job["id"])
                self.assertEqual(restored["status"], "failed")
                self.assertEqual(restored["recovery"], job["recovery"])
                delivery = restarted_store.delivery(key)
                self.assertEqual(delivery["status"], "skipped")
                self.assertFalse(delivery["retryable"])
                self.assertEqual(delivery["skip_reason"], "upstream_pending")
                with self.assertRaisesRegex(ValueError, "可能已受理"):
                    await restarted.web_retry_job({"id": job["id"]})
            finally:
                await restarted.terminate()
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.context.sent)


if __name__ == "__main__":
    unittest.main()
