"""Native photo acceptance means local validity, never upstream acceptance.

Real adapter preparation is exercised here. Background submission is suppressed,
so these cases neither contact an image service nor consume generation quota.
"""
import copy
import io
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

import test_core as fixtures
import test_tool_failures as conditions_fixtures
from test_comfyui import FIELDS, WORKFLOW
from test_core import Event, image_bytes
from canvas_test import main
from canvas_test.providers.base import provider_from_config


class NativePreflight0510Tests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    seed_pending = conditions_fixtures.GroupConfirmationTests.seed_pending

    async def asyncSetUp(self):
        await fixtures.RuntimeTests.asyncSetUp(self)
        # Undo the fixture's duck-typed adapter and use production selection.
        self.plugin._provider = types.MethodType(main.PersonaCanvasPlugin._provider, self.plugin)
        self.adapters = []
        self.spawned = []

        def factory(name, config):
            item = provider_from_config(name, config)
            for method in ("_request", "_post", "_http"):
                setattr(item, method, AsyncMock(side_effect=AssertionError("preflight must not contact upstream")))
            if callable(getattr(item, "preflight", None)):
                item.preflight = Mock(wraps=item.preflight)
            self.adapters.append(item)
            return item

        def local_queue(coroutine, *args):
            self.spawned.append(args)
            coroutine.close()

        self.factory_patch = patch.object(main, "provider_from_config", side_effect=factory)
        self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
        self.spawn_patch = patch.object(self.plugin, "_spawn", side_effect=local_queue)
        self.spawn_patch.start()
        self.addCleanup(self.spawn_patch.stop)
        self.reserve_patch = patch.object(self.plugin.moderation, "reserve", wraps=self.plugin.moderation.reserve)
        self.reserve = self.reserve_patch.start()
        self.addCleanup(self.reserve_patch.stop)

    def configure(self, kind="openai", *, name="drawing", **values):
        config = {"kind": kind, "endpoint": "https://drawing.example", "model": "gpt-image-1.5", **values}
        self.store.settings["providers"][name] = config
        self.store.settings["default_provider"] = name
        return config

    async def photo(self, event=None, **kwargs):
        event = event or Event("按刚才说好的来一张吧")
        return json.loads(await self.plugin.tool_photo(
            event, "smiling portrait", request_summary="用户结合上下文明确要求这次拍照，角色愿意。", **kwargs,
        ))

    def assert_no_submission(self):
        self.reserve.assert_not_called()
        for adapter in self.adapters:
            for method in ("_request", "_post", "_http"):
                getattr(adapter, method).assert_not_awaited()

    def assert_rejected_before_queue(self, result, reason=""):
        self.assertFalse(result["ok"], result)
        if reason:
            self.assertIn(reason, result.get("reason", ""))
        self.assertTrue(result.get("reason"), result)
        self.assertFalse(self.store.recent_jobs(), result)
        self.assertFalse(self.spawned, result)
        self.assert_no_submission()

    def assert_local_queue(self, result):
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["phase"], "local_queued")
        self.assertIs(result["submitted"], False)
        self.assertIs(result["upstream_accepted"], False)
        self.assertIs(result["image_sent"], False)
        self.assertIn("本地任务", result["instruction"])
        self.assertIn("等待", result["instruction"])
        self.assertNotIn("照片正在生成", result["instruction"])
        self.assertNotIn("可以简短说正在拍", result["instruction"])
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.store.job(result["job_id"])["status"], "queued")
        self.assert_no_submission()

    async def test_openai_empty_model_is_immediate_error_without_job(self):
        self.configure(model="")
        self.assert_rejected_before_queue(await self.photo(), "模型")

    async def test_gemini_empty_model_is_immediate_error_without_job(self):
        self.configure("gemini", model="   ")
        self.assert_rejected_before_queue(await self.photo(), "模型")

    async def test_unconfigured_installation_does_not_claim_generation_started(self):
        self.assert_rejected_before_queue(await self.photo())

    async def test_missing_selected_provider_is_immediate_error(self):
        self.store.settings["default_provider"] = "removed-provider"
        self.assert_rejected_before_queue(await self.photo(), "接口")

    async def test_unknown_provider_kind_is_immediate_error(self):
        self.configure("unknown-image-adapter")
        self.assert_rejected_before_queue(await self.photo())

    async def test_selected_default_is_checked_instead_of_unselected_valid_config(self):
        self.configure(name="valid-but-unselected")
        self.configure(name="selected-invalid", model="")
        self.assert_rejected_before_queue(await self.photo(), "模型")
        self.assertEqual([item.name for item in self.adapters], ["selected-invalid"])

    async def test_valid_selected_provider_ignores_unconfigured_default_entry(self):
        self.configure(name="selected-valid")
        result = await self.photo()
        self.assert_local_queue(result)
        self.assertEqual([item.name for item in self.adapters], ["selected-valid"])
        self.adapters[0].preflight.assert_called_once()

    async def test_invalid_extra_body_is_rejected_before_job(self):
        self.configure(extra_body={"prompt": "override role prompt"})
        self.assert_rejected_before_queue(await self.photo())

    async def test_valid_gemini_request_stays_local_before_background_submission(self):
        self.configure("gemini", model="gemini-2.5-flash-image")
        self.assert_local_queue(await self.photo())
        self.adapters[0].preflight.assert_called_once()

    async def test_custom_endpoint_can_use_empty_model_when_fields_are_valid(self):
        self.configure("custom", model="")
        self.assert_local_queue(await self.photo())
        self.adapters[0].preflight.assert_called_once()

    async def test_custom_conflicting_fields_are_rejected_before_job(self):
        self.configure("custom", model="", model_field="prompt")
        self.assert_rejected_before_queue(await self.photo(), "冲突")

    async def test_comfyui_valid_workflow_accepts_empty_display_model(self):
        self.configure("comfyui", endpoint="http://127.0.0.1:8188", model="", workflow=copy.deepcopy(WORKFLOW), workflow_fields=copy.deepcopy(FIELDS))
        result = await self.photo()
        self.assert_local_queue(result)
        self.adapters[0].preflight.assert_called_once()

    async def test_comfyui_invalid_prompt_mapping_is_immediate_error(self):
        fields = {**FIELDS, "prompt": "missing.inputs.text"}
        self.configure("comfyui", endpoint="http://127.0.0.1:8188", model="", workflow=copy.deepcopy(WORKFLOW), workflow_fields=fields)
        self.assert_rejected_before_queue(await self.photo())

    async def test_unsupported_reference_edit_fails_without_queue(self):
        self.configure(model="dall-e-3")
        event = Event("把上一张的背景改成夜景")
        env = await self.plugin.dialogue.environment(event.unified_msg_origin)
        session = self.store.session(env["key"])
        session["last_image"] = self.store.save_asset(image_bytes(), "png").name
        self.store.save_session(env["key"], session)
        self.assert_rejected_before_queue(await self.photo(event, mode="edit", use_last_image=True), "参考图")

    async def test_reference_pixels_are_preflighted_before_queue(self):
        self.configure(model="dall-e-2", supports_image_edit=True)
        event = Event("把上一张背景换掉")
        env = await self.plugin.dialogue.environment(event.unified_msg_origin)
        buffer = io.BytesIO()
        Image.new("RGB", (32, 16), "blue").save(buffer, "PNG")
        session = self.store.session(env["key"])
        session["last_image"] = self.store.save_asset(buffer.getvalue(), "png").name
        self.store.save_session(env["key"], session)
        self.assert_rejected_before_queue(await self.photo(event, mode="edit", use_last_image=True))
        self.adapters[0].preflight.assert_called_once()

    async def test_chat_model_receives_local_queue_rules_without_generation_claims(self):
        request = types.SimpleNamespace(system_prompt="existing persona")
        await self.plugin.inject_visual_state(Event("随便聊聊"), request)
        self.assertEqual(request.system_prompt, "existing persona")
        self.assertIn("queued 为本地等待", request.extra_user_content_parts[-1].text)
        self.assertIn("不证明上游已受理", request.extra_user_content_parts[-1].text)
        self.assertNotIn("queued/generating 仅为正在生成", request.extra_user_content_parts[-1].text)
        self.assertFalse(self.store.recent_jobs())

    async def test_failed_local_preflight_preserves_conditions_and_allows_next_turn(self):
        self.configure(model="")
        event = Event("我接受原来的全部拍摄约定")
        key, pending = await self.seed_pending(event)
        result = await self.photo(event, confirmed_request_id=pending["request_id"])
        self.assert_rejected_before_queue(result, "模型")
        saved = self.store.session(key)["pending"]
        self.assertEqual(saved["request_id"], pending["request_id"])
        self.assertEqual(saved["requirements"], pending["requirements"])
        self.assertFalse(saved.get("execution_job_id"))

        self.store.settings["providers"]["drawing"]["model"] = "gpt-image-1.5"
        next_turn = Event("设置改好了，就按原先的约定来")
        queued = await self.photo(next_turn, confirmed_request_id=pending["request_id"])
        self.assert_local_queue(queued)
        saved = self.store.session(key)["pending"]
        self.assertEqual(saved["request_id"], pending["request_id"])
        self.assertEqual(saved["execution_job_id"], queued["job_id"])
        self.assertEqual(len(self.store.recent_jobs()), 1)


if __name__ == "__main__":
    unittest.main()
