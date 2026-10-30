"""Saved ComfyUI task recovery with simulated HTTP and no generation service."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from test_comfyui import PNG, ProviderError, history, json_reply, provider, working_http


class ComfyRecovery0516Tests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_and_acceptance_are_persisted_before_waiting_for_output(self):
        item = provider()
        records = []
        item.on_progress = AsyncMock(side_effect=lambda record: records.append(copy.deepcopy(record)))
        original_http = working_http(item).side_effect

        async def ordered_http(method, url, body=None, **kwargs):
            if url.endswith("/prompt"):
                self.assertEqual(records[-1]["phase"], "submitting")
                self.assertFalse(records[-1]["upstream_accepted"])
            if "/history/" in url:
                self.assertEqual(records[-1]["prompt_id"], "task-123")
                self.assertEqual(records[-1]["phase"], "accepted")
                self.assertTrue(records[-1]["upstream_accepted"])
            return await original_http(method, url, body, **kwargs)

        item._http.side_effect = ordered_http
        result = await item.generate("portrait", "watermark")
        self.assertEqual(result.data, PNG)
        self.assertEqual([r["phase"] for r in records], ["submitting", "accepted", "fetching", "completed"])
        self.assertEqual(records[-1]["fingerprint"], item.recovery_fingerprint())
        self.assertNotIn("workflow", json.dumps(records))
        self.assertNotIn("portrait", json.dumps(records))

    async def test_missing_submission_response_remains_unknown_without_reliable_id(self):
        item = provider()
        item.on_progress = AsyncMock()
        item._http = AsyncMock(side_effect=ProviderError("connection lost"))
        with self.assertRaises(ProviderError):
            await item.generate("portrait", "")
        self.assertEqual(item.last_recovery["phase"], "submitting")
        self.assertEqual(item.last_recovery["prompt_id"], "")
        self.assertTrue(item.last_recovery["submitted"])
        self.assertFalse(item.last_recovery["upstream_accepted"])
        self.assertEqual(item._http.call_count, 1)

    async def test_new_provider_recovers_existing_image_using_get_requests_only(self):
        original = provider()
        fingerprint = original.recovery_fingerprint()
        restarted = provider()
        calls = working_http(restarted)
        result = await restarted.recover("task-123", fingerprint=fingerprint)
        self.assertEqual(result.data, PNG)
        self.assertEqual(restarted.last_recovery["phase"], "completed")
        self.assertEqual(restarted.last_recovery["prompt_id"], "task-123")
        self.assertEqual([call.args[0] for call in calls.call_args_list], ["GET", "GET"])
        self.assertTrue(calls.call_args_list[0].args[1].endswith("/history/task-123"))
        self.assertIn("/view?", calls.call_args_list[1].args[1])

    async def test_queue_states_are_read_only_and_do_not_return_an_image(self):
        for phase, field in (("queued", "queue_pending"), ("running", "queue_running")):
            with self.subTest(phase=phase):
                item = provider()
                queue = {"queue_running": [], "queue_pending": []}
                queue[field] = [[0, "task-123", {"secret": "never-return-this-workflow"}, {}]]

                async def http(method, url, body=None, **kwargs):
                    self.assertEqual(method, "GET")
                    return json_reply(queue if url.endswith("/queue") else {})

                item._http = AsyncMock(side_effect=http)
                result = await item.recover("task-123", fingerprint=item.recovery_fingerprint())
                self.assertIsNone(result)
                self.assertEqual(item.last_recovery["phase"], phase)
                self.assertNotIn("never-return", json.dumps(item.last_recovery))
                self.assertEqual(item._http.call_count, 2)

    async def test_missing_task_is_unknown_and_rechecks_history_without_submission(self):
        item = provider()

        async def http(method, url, body=None, **kwargs):
            self.assertEqual(method, "GET")
            return json_reply({"queue_running": [], "queue_pending": []} if url.endswith("/queue") else {})

        item._http = AsyncMock(side_effect=http)
        self.assertIsNone(await item.recover("task-123", fingerprint=item.recovery_fingerprint()))
        self.assertEqual(item.last_recovery["phase"], "missing")
        self.assertEqual(item._http.call_count, 3)
        self.assertEqual(sum("/history/" in c.args[1] for c in item._http.call_args_list), 2)

    async def test_task_completing_between_history_and_queue_is_recovered(self):
        item = provider()
        reads = 0

        async def http(method, url, body=None, **kwargs):
            nonlocal reads
            self.assertEqual(method, "GET")
            if "/history/" in url:
                reads += 1
                return json_reply({} if reads == 1 else history())
            if url.endswith("/queue"):
                return json_reply({"queue_running": [], "queue_pending": []})
            return PNG, "image/png", 200

        item._http = AsyncMock(side_effect=http)
        result = await item.recover("task-123", fingerprint=item.recovery_fingerprint())
        self.assertEqual(result.data, PNG)
        self.assertEqual(item.last_recovery["phase"], "completed")
        self.assertEqual(item._http.call_count, 4)

    async def test_recovery_rejects_wrong_binding_or_id_before_network_io(self):
        original = provider()
        fingerprint = original.recovery_fingerprint()
        for change in ({"endpoint": "http://127.0.0.1:8189"}, {"output_node": "9"}, {"workflow_fields": {"prompt": "7.inputs.text"}}):
            item = provider(**change)
            item._http = AsyncMock()
            with self.subTest(change=change), self.assertRaisesRegex(ProviderError, "已改变"):
                await item.recover("task-123", fingerprint=fingerprint)
            item._http.assert_not_called()
        original._http = AsyncMock()
        for prompt_id in ("", "../task-123", "https://service/task-123", "a" * 129, None):
            with self.subTest(prompt_id=prompt_id), self.assertRaises(ProviderError):
                await original.recover(prompt_id, fingerprint=fingerprint)
        original._http.assert_not_called()

    async def test_fingerprint_hashes_node_secrets_and_ignores_header_key_rotation(self):
        item = provider(api_key="header-old")
        item.config["workflow"]["6"]["inputs"]["api_key"] = "private-node-secret"
        fingerprint = item.recovery_fingerprint()
        self.assertEqual(len(fingerprint), 64)
        self.assertNotIn("private", fingerprint)
        item.config["api_key"] = "header-new"
        item.config["timeout"] = 20
        self.assertEqual(item.recovery_fingerprint(), fingerprint)
        item.config["workflow"]["6"]["inputs"]["api_key"] = "different-node-secret"
        self.assertNotEqual(item.recovery_fingerprint(), fingerprint)

    async def test_remote_errors_and_invalid_images_never_claim_completed(self):
        for body, image, mime in ((history(status={"status_str": "error", "messages": [["execution_error", {"message": "private-node-secret"}]]}), PNG, "image/png"), (history(), b"not an image", "image/png"), (history(), PNG, "text/html")):
            item = provider()
            calls = working_http(item, result_history=body, view=image, mime=mime)
            with self.subTest(body=body, mime=mime), self.assertRaises(ProviderError) as error:
                await item.recover("task-123", fingerprint=item.recovery_fingerprint())
            self.assertNotIn("private-node-secret", str(error.exception))
            self.assertNotEqual(item.last_recovery.get("phase"), "completed")
            self.assertTrue(all(call.args[0] == "GET" for call in calls.call_args_list))

    async def test_recovery_timeout_does_not_cancel_or_resubmit_original_task(self):
        item = provider()

        async def delayed(*args, **kwargs):
            await asyncio.Event().wait()

        item._http = AsyncMock(side_effect=delayed)
        original_wait = asyncio.wait_for

        async def short_wait(coro, timeout):
            return await original_wait(coro, .01)

        with patch("providers.comfyui.asyncio.wait_for", side_effect=short_wait):
            with self.assertRaisesRegex(ProviderError, "原任务查询超时"):
                await item.recover("task-123", fingerprint=item.recovery_fingerprint())
        self.assertEqual(item._http.call_count, 1)
        self.assertEqual(item._http.call_args.args[0], "GET")

    async def test_callback_receives_copy_and_failure_preserves_accepted_id(self):
        item = provider()
        calls = working_http(item)

        async def persist(record):
            if record["phase"] == "accepted":
                record["prompt_id"] = "mutated"
                raise RuntimeError("storage unavailable")

        item.on_progress = persist
        with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
            await item.generate("portrait", "")
        self.assertEqual(item.last_recovery["prompt_id"], "task-123")
        self.assertEqual(item.last_request["workflow_prompt_id"], "task-123")
        self.assertTrue(item.last_request["upstream_accepted"])
        self.assertEqual(calls.call_count, 1)


if __name__ == "__main__":
    unittest.main()
