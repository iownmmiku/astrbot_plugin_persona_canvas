"""Offline integration contracts for editable provider settings and diagnostics."""
import asyncio
import base64
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import test_core as fixtures
from canvas_test.providers.base import ProviderError, provider_from_config


class RequestDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    def provider(self, *, timeout=300, error=None):
        provider = provider_from_config("drawing", {"kind": "openai", "endpoint": "https://offline.invalid/v1", "model": "anime-model", "timeout": timeout, "negative_mode": "field"})
        body = {"data": [{"b64_json": base64.b64encode(fixtures.image_bytes()).decode()}]}
        provider._post = AsyncMock(side_effect=error, return_value=(json.dumps(body).encode(), "application/json"))
        self.plugin._provider = lambda name=None: provider
        return provider

    async def test_conflicting_custom_paths_cannot_modify_saved_settings(self):
        before = copy.deepcopy(self.store.settings)
        for mapping in ({"width": "params.size", "height": "params.size"}, {"width": "params.size", "height": "params.size.height"}):
            with self.subTest(mapping=mapping), self.assertRaises(ProviderError):
                await self.plugin.web_save_provider({"name": "invalid", "kind": "custom", "endpoint": "https://offline.invalid", "option_fields": mapping, "set_default": True})
            self.assertEqual(self.store.settings, before)

    async def test_negative_modes_roundtrip_and_gemini_rejects_independent_field(self):
        result = await self.plugin.web_save_provider({"name": "compat", "kind": "openai", "endpoint": "https://offline.invalid", "negative_mode": "field", "negative_prompt_field": "params.negative"})
        self.assertEqual(result["negative_mode"], "field")
        self.assertEqual(result["negative_prompt_field"], "params.negative")
        before = copy.deepcopy(self.store.settings)
        with self.assertRaises(ProviderError):
            await self.plugin.web_save_provider({"name": "gemini", "kind": "gemini", "negative_mode": "field", "set_default": True})
        self.assertEqual(self.store.settings, before)

    async def test_generation_timeout_accepts_600_without_extending_chat_timeout(self):
        result = await self.plugin.web_save_settings({"generation": {"timeout_sec": 600}})
        self.assertEqual(result["generation"]["timeout_sec"], 600)
        with self.assertRaises(ValueError):
            await self.plugin.web_save_settings({"dialogue": {"timeout_sec": 600}})

    async def test_test_image_and_chat_share_actual_request_and_effective_timeout(self):
        provider = self.provider(timeout=300)
        self.store.settings["generation"]["timeout_sec"] = 180
        seen = []
        async def capture(awaitable, timeout):
            seen.append(timeout)
            return await awaitable
        with patch.object(fixtures.main.asyncio, "wait_for", side_effect=capture):
            test = await self.plugin._image_test({"generate_image": True, "prompt": "flower"})
            env = await self.plugin.dialogue.environment("webui:diagnostics")
            job = await self.plugin._generate_job(env, "portrait", is_admin=True)
        self.assertTrue(test["ok"], test)
        self.assertEqual(seen, [180, 180])
        for summary in (test["request_summary"], job["request_summary"]):
            self.assertEqual(summary["effective_timeout_sec"], 180)
            self.assertEqual(summary["provider_timeout_sec"], 300)
            self.assertEqual(summary["task_timeout_sec"], 180)
            self.assertEqual(summary["negative_mode"], "field")
        self.assertEqual(test["request_summary"]["prompt"], "flower")
        self.assertEqual(job["request_summary"]["prompt"], provider._post.call_args.args[1]["prompt"])

    async def test_failed_test_image_preserves_submitted_request(self):
        provider = self.provider(error=ProviderError("offline HTTP failure"))
        result = await self.plugin._image_test({"generate_image": True, "prompt": "flower"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["request_summary"]["prompt"], provider._post.call_args.args[1]["prompt"])
        self.assertIn("offline HTTP failure", result["error"])

    async def test_timeout_test_image_reports_limit_without_claiming_submission(self):
        self.provider(timeout=90)
        async def timeout(awaitable, seconds):
            self.assertEqual(seconds, 90)
            awaitable.close()
            raise asyncio.TimeoutError()
        with patch.object(fixtures.main.asyncio, "wait_for", side_effect=timeout):
            result = await self.plugin._image_test({"generate_image": True})
        self.assertFalse(result["ok"])
        self.assertIn("90", result["error"])
        self.assertIn("尚未提交", result["request_summary"]["notes"])

    async def test_gemini_workbench_options_reach_generation_unchanged(self):
        env = await self.plugin.dialogue.environment("webui:gemini")
        decision = {"decision": "photo", "prompt": "portrait", "reply": "正在拍", "state_patch": {}}
        result = await self.plugin._queue_job(env, decision, "自拍", {"aspect_ratio": "9:16", "image_size": "2K", "_explicit": ["aspect_ratio", "image_size"]})
        await asyncio.gather(*list(self.plugin._tasks))
        options = self.image_provider.calls[0]["options"]
        self.assertEqual(options["aspect_ratio"], "9:16")
        self.assertEqual(options["image_size"], "2K")
        self.assertEqual(set(options["_explicit"]), {"aspect_ratio", "image_size"})
        self.assertEqual(self.store.job(result["job_id"])["status"], "succeeded")

    async def test_reference_failure_keeps_prepared_request_and_accurate_stage(self):
        asset = self.store.save_asset(fixtures.image_bytes(), "png").name
        persona = copy.deepcopy(self.store.persona())
        persona.update(reference_enabled=True, reference_asset=asset)
        self.store.upsert_persona(persona)
        self.image_provider.capabilities.image_to_image = False
        queued = json.loads(await self.plugin.tool_photo(fixtures.Event("拍一张自拍"), "portrait"))
        await asyncio.gather(*list(self.plugin._tasks), return_exceptions=True)
        history = self.store.recent_history()[0]
        self.assertFalse(history["ok"])
        self.assertEqual(history["job_id"], queued["job_id"])
        self.assertEqual(history["failure_stage"], "reference")
        self.assertIn("尚未提交", history["request_summary"]["notes"])
        self.assertIn("portrait", history["prompt"])
        self.assertFalse(self.image_provider.calls)


if __name__ == "__main__":
    unittest.main()
