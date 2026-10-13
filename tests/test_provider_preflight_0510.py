"""Local provider preflight contracts; all HTTP/upload entry points are forbidden."""
import base64
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from providers.base import ProviderError, provider_from_config
from storage import DEFAULT_SETTINGS
from test_comfyui import FIELDS, PNG, WORKFLOW


def adapter(kind="openai", **overrides):
    config = {"kind": kind, "endpoint": "https://drawing.example/v1", "model": "gpt-image-1.5", **overrides}
    if kind == "comfyui":
        config = {"endpoint": "http://127.0.0.1:8188", "workflow": copy.deepcopy(WORKFLOW), "workflow_fields": copy.deepcopy(FIELDS), **config, **overrides}
    item = provider_from_config("drawing", config)
    item._http = AsyncMock(side_effect=AssertionError("Preflight must never issue HTTP"))
    item._post = AsyncMock(side_effect=AssertionError("Preflight must never submit generation"))
    return item


class ProviderPreflightContracts(unittest.IsolatedAsyncioTestCase):
    def assert_offline(self, item):
        item._http.assert_not_called()
        item._post.assert_not_called()

    def test_all_native_adapters_accept_shared_defaults_without_mutation(self):
        for kind in ("openai", "gemini", "novelai", "custom", "comfyui"):
            with self.subTest(kind=kind):
                item = adapter(kind)
                options = copy.deepcopy(DEFAULT_SETTINGS["generation"])
                before_options, before_config = copy.deepcopy(options), copy.deepcopy(item.config)
                diagnostic = {"submitted": True, "prompt": "previous real request"}
                item.last_request = diagnostic
                with patch("providers.base.secrets.randbelow", side_effect=AssertionError("No random seed in preflight")), patch("providers.comfyui.secrets.randbits", side_effect=AssertionError("No random seed in preflight")), patch("providers.comfyui.secrets.token_hex", side_effect=AssertionError("No client id in preflight")):
                    self.assertIsNone(item.preflight("a seaside portrait", "bad anatomy", options=options))
                self.assertIs(item.last_request, diagnostic)
                self.assertEqual(options, before_options)
                self.assertEqual(item.config, before_config)
                self.assert_offline(item)

    def test_required_models_are_checked_locally(self):
        for kind, model in (("openai", ""), ("openai", "  "), ("gemini", ""), ("gemini", "models/")):
            with self.subTest(kind=kind, model=model):
                item = adapter(kind, model=model)
                with self.assertRaises(ProviderError):
                    item.validate_config()
                with self.assertRaises(ProviderError):
                    item.preflight("portrait", "")
                self.assertEqual(item.last_request, {})
                self.assert_offline(item)

    def test_workflow_and_custom_can_omit_model(self):
        for kind in ("custom", "comfyui"):
            with self.subTest(kind=kind):
                item = adapter(kind, model="")
                item.validate_config()
                self.assertIsNone(item.preflight("portrait", "", options=copy.deepcopy(DEFAULT_SETTINGS["generation"])))
                self.assertEqual(item.last_request, {})
                self.assert_offline(item)

    async def test_local_failures_match_generate_before_http(self):
        cases = [
            ("openai", {}, {"width": 832, "height": 1216, "_explicit": ["width", "height"]}),
            ("openai", {}, {"steps": 30, "_explicit": ["steps"]}),
            ("openai", {"extra_body": {"prompt": "overwritten"}}, {}),
            ("gemini", {}, {"width": 512, "_explicit": ["width"]}),
            ("gemini", {}, {"image_size": "8K", "_explicit": ["image_size"]}),
            ("gemini", {}, {"aspect_ratio": "0:1"}),
            ("novelai", {}, {"width": 513}),
            ("novelai", {}, {"steps": 0}),
            ("novelai", {}, {"strength": 0.5, "_explicit": ["strength"]}),
            ("custom", {"option_fields": {"width": ""}}, {"width": 512, "_explicit": ["width"]}),
            ("custom", {"option_fields": {"width": "prompt"}}, {}),
            ("custom", {"supports_sampler": True}, {"scale": float("nan")}),
            ("comfyui", {"workflow_fields": {"prompt": "6.inputs.text"}}, {"width": 512, "_explicit": ["width"]}),
            ("comfyui", {}, {"width": 8192, "height": 8192}),
            ("comfyui", {}, {"seed": 2 ** 64}),
            ("comfyui", {"workflow_fields": {"prompt": "3.inputs.positive"}}, {}),
        ]
        for kind, config, options in cases:
            with self.subTest(kind=kind, config=config, options=options):
                item = adapter(kind, **config)
                with self.assertRaises(ProviderError) as local:
                    item.preflight("portrait", "", options=options)
                with self.assertRaises(ProviderError) as runtime:
                    await item.generate("portrait", "", options=options)
                self.assertEqual(str(local.exception), str(runtime.exception))
                self.assertFalse(item.last_request.get("submitted", False))
                self.assert_offline(item)

    def test_invalid_endpoint_and_route_are_preflight_errors(self):
        for config in ({"endpoint": ""}, {"endpoint": "file:///tmp/photo"}, {"endpoint": "https://drawing.example:invalid"}, {"generation_path": "https://other.example/generate"}, {"timeout": 0}, {"extra_body": ["invalid"]}):
            with self.subTest(config=config):
                item = adapter(**config)
                with self.assertRaises(ProviderError):
                    item.preflight("portrait", "")
                self.assert_offline(item)

    def test_reference_capability_and_real_pixels_are_checked_locally(self):
        for kind in ("openai", "gemini", "novelai", "custom", "comfyui"):
            with self.subTest(kind=kind):
                item = adapter(kind)
                with self.assertRaises(ProviderError):
                    item.preflight("portrait", "", reference=PNG)
                config = {"supports_image_edit": True}
                if kind == "comfyui":
                    config["workflow_fields"] = {**FIELDS, "reference": "10.inputs.image"}
                item = adapter(kind, **config)
                with self.assertRaises(ProviderError):
                    item.preflight("portrait", "", reference=b"not an image")
                self.assertIsNone(item.preflight("portrait", "", reference=PNG))
                self.assertEqual(item.last_request, {})
                self.assert_offline(item)

    def test_workflow_reference_preflight_does_not_upload_or_replace_config(self):
        item = adapter("comfyui", workflow_fields={**FIELDS, "reference": "10.inputs.image"})
        before = copy.deepcopy(item.config)
        with patch.object(item, "_json_request", new=AsyncMock(side_effect=AssertionError("No reference upload"))) as request:
            item.preflight("portrait", "", reference=PNG, options={"seed": -1})
            request.assert_not_called()
        self.assertEqual(item.config, before)
        self.assertEqual(item.last_request, {})

    def test_too_many_references_and_dalle_reference_format_fail_locally(self):
        for kind in ("novelai", "custom", "comfyui"):
            with self.subTest(kind=kind):
                config = {"supports_image_edit": True}
                if kind == "comfyui":
                    config["workflow_fields"] = {**FIELDS, "reference": "10.inputs.image"}
                item = adapter(kind, **config)
                with self.assertRaises(ProviderError):
                    item.preflight("portrait", "", reference=[PNG, PNG])
                self.assert_offline(item)
        item = adapter("openai", model="dall-e-2", supports_image_edit=True)
        from test_comfyui import JPEG
        with self.assertRaisesRegex(ProviderError, "正方形 PNG"):
            item.preflight("portrait", "", reference=JPEG)
        self.assert_offline(item)

    def test_request_validation_does_not_erase_previous_diagnostics(self):
        item = adapter()
        item.last_request = {"submitted": True, "model": "previous-model"}
        for kwargs in ({"prompt": ""}, {"options": []}, {"options": {"_explicit": "width"}}, {"options": {"seed": 42}}):
            request = {"prompt": "portrait", "negative_prompt": "", **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(ProviderError):
                item.preflight(**request)
            self.assertEqual(item.last_request, {"submitted": True, "model": "previous-model"})
        self.assert_offline(item)

    async def test_valid_preflight_still_allows_normal_generation(self):
        item = adapter()
        item._post = AsyncMock(return_value=(json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode(), "application/json"))
        options = {"size": "1024x1536", "_explicit": ["size"]}
        item.preflight("portrait", "", options=options)
        self.assertEqual(item.last_request, {})
        result = await item.generate("portrait", "", options=options)
        self.assertEqual(result.data, PNG)
        self.assertTrue(item.last_request["submitted"])
        item._post.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
