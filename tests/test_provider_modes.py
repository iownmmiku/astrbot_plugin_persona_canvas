"""Offline negative-channel, configuration, and inspectable-request contracts."""
import base64
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1]))
from providers.base import GeminiProvider, ProviderError, provider_from_config


def picture(fmt="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(output, format=fmt)
    return output.getvalue()


PNG = picture()
JPEG = picture("JPEG")
ENCODED = base64.b64encode(PNG).decode("ascii")
JSON_RESPONSE = (json.dumps({"data": [{"b64_json": ENCODED}]}).encode(), "application/json")
GEMINI_RESPONSE = (json.dumps({"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": ENCODED}}]}}]}).encode(), "application/json")


def provider(kind="openai", **config):
    item = provider_from_config("drawing", {"kind": kind, "endpoint": "https://provider.example", "model": "gpt-image-1.5" if kind == "openai" else "nai-diffusion-4-5-full" if kind == "novelai" else "model", **config})
    item._post = AsyncMock(return_value=GEMINI_RESPONSE if kind == "gemini" else JSON_RESPONSE)
    return item


class NegativeModeContracts(unittest.IsolatedAsyncioTestCase):
    async def test_old_boolean_never_enables_openai_or_gemini_channel(self):
        for kind in ("openai", "gemini"):
            for flag in (None, False, True):
                with self.subTest(kind=kind, old_flag=flag):
                    item = provider(kind, **({"negative_prompt": flag} if flag is not None else {}))
                    await item.generate("portrait", "abs, muscular female")
                    self.assertEqual(item.capabilities.negative_mode, "disabled")
                    self.assertFalse(item.capabilities.negative_prompt)
                    self.assertEqual(item.last_request["prompt"], "portrait")
                    self.assertEqual(item.last_request["negative_prompt"], "")
                    self.assertNotIn("muscular", json.dumps(item._post.call_args.args[1]))

    async def test_custom_legacy_independent_field_is_preserved(self):
        item = provider("custom", negative_prompt=True, negative_prompt_field="request.negative")
        await item.generate("portrait", "abs")
        self.assertTrue(item.capabilities.negative_prompt)
        self.assertEqual(item.capabilities.negative_mode, "field")
        self.assertEqual(item._post.call_args.args[1]["request"]["negative"], "abs")
        self.assertEqual(item.last_request["negative_prompt_field"], "request.negative")

    async def test_explicit_disabled_overrides_custom_legacy_boolean(self):
        item = provider("custom", negative_prompt=True, negative_mode="disabled")
        await item.generate("portrait", "abs")
        self.assertEqual(item.last_request["prompt"], "portrait")
        self.assertNotIn("negative_prompt", item._post.call_args.args[1])
        self.assertFalse(item.capabilities.negative_prompt)

    async def test_natural_language_requires_explicit_choice(self):
        for kind in ("openai", "gemini", "custom"):
            with self.subTest(kind=kind):
                item = provider(kind, negative_mode="natural_language")
                await item.generate("portrait", "watermark")
                self.assertIn("Avoid these visual elements and defects: watermark", item.last_request["prompt"])
                self.assertEqual(item.last_request["negative_mode"], "natural_language")
                self.assertEqual(item.last_request["negative_prompt"], "")
                self.assertFalse(item.capabilities.negative_prompt)

    async def test_novelai_always_uses_native_negative_channels(self):
        for mode in (None, "disabled", "natural_language", "field"):
            with self.subTest(mode=mode):
                item = provider("novelai", **({"negative_mode": mode} if mode else {}))
                await item.generate("portrait", "abs", options={"width": 512, "height": 512})
                body = item._post.call_args.args[1]
                self.assertEqual(body["input"], "portrait")
                self.assertEqual(body["parameters"]["negative_prompt"], "abs")
                self.assertEqual(body["parameters"]["v4_negative_prompt"]["caption"]["base_caption"], "abs")
                self.assertEqual(item.last_request["negative_mode"], "field")
                self.assertEqual(item.last_request["negative_prompt"], "abs")
                self.assertEqual(item.last_request["negative_prompt_field"], "parameters.negative_prompt")

    def test_gemini_rejects_independent_field_even_with_direct_constructor(self):
        for constructor in (lambda: provider("gemini", negative_mode="field"), lambda: GeminiProvider("direct", {"negative_mode": "field"})):
            with self.assertRaisesRegex(ProviderError, "Gemini.*不支持独立负面字段"):
                constructor()

    def test_unknown_mode_rejects_configuration(self):
        with self.assertRaisesRegex(ProviderError, "negative_mode"):
            provider(negative_mode="guess")

    async def test_openai_compatible_json_independent_field(self):
        item = provider(model="anime-model", negative_mode="field")
        await item.generate("portrait", "abs, muscular female")
        body = item._post.call_args.args[1]
        self.assertEqual(body["prompt"], "portrait")
        self.assertEqual(body["negative_prompt"], "abs, muscular female")
        self.assertEqual(item.last_request["negative_prompt_field"], "negative_prompt")
        self.assertEqual(item.last_request["negative_source"], "plugin")
        self.assertTrue(item.capabilities.negative_prompt)

    async def test_openai_compatible_edit_uses_same_nested_negative_field(self):
        item = provider(negative_mode="field", negative_prompt_field="parameters.negative", supports_image_edit=True)
        await item.generate("portrait", "abs", reference=JPEG)
        body = item._post.call_args.args[1]
        self.assertEqual(body["parameters"]["negative"], "abs")
        self.assertEqual(item._post.call_args.kwargs["files"], {"image": JPEG})
        self.assertEqual(item.last_request["reference_count"], 1)
        self.assertEqual(item.last_request["negative_prompt_field"], "parameters.negative")

    async def test_negative_field_is_protected_against_extra_body(self):
        for extra in ({"parameters": {"negative": "other"}}, {"parameters": "other"}):
            with self.subTest(extra=extra):
                item = provider(negative_mode="field", negative_prompt_field="parameters.negative", extra_body=extra)
                with self.assertRaisesRegex(ProviderError, "extra_body"):
                    await item.generate("portrait", "abs")
                item._post.assert_not_called()

    def test_openai_negative_field_cannot_replace_core_parameters(self):
        for field in ("prompt", "size", "image.caption", "parameters..negative"):
            with self.subTest(field=field), self.assertRaises(ProviderError):
                provider(negative_mode="field", negative_prompt_field=field).validate_config()

    async def test_explicit_extra_negative_is_visible_but_not_implicitly_replaced(self):
        for kind in ("openai", "custom", "gemini"):
            with self.subTest(kind=kind):
                item = provider(kind, negative_mode="disabled", negative_prompt_field="parameters.negative", extra_body={"parameters": {"negative": "manually configured"}})
                await item.generate("portrait", "persona negative")
                self.assertEqual(item.last_request["negative_mode"], "disabled")
                self.assertEqual(item.last_request["negative_prompt"], "manually configured")
                self.assertEqual(item.last_request["negative_source"], "extra_body")
                self.assertNotIn("persona negative", json.dumps(item._post.call_args.args[1]))


class MappingAndRequestSummaryContracts(unittest.IsolatedAsyncioTestCase):
    def test_validate_config_rejects_duplicate_or_ancestor_option_paths(self):
        for fields in ({"width": "params.dimension", "height": "params.dimension"}, {"width": "params.dimension", "height": "params.dimension.height"}, {"width": "params", "height": "params.height"}, {"width": "prompt"}, {"width": "image.width"}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ProviderError, "字段映射冲突"):
                provider("custom", option_fields=fields).validate_config()

    async def test_mapping_collision_fails_before_request_even_without_options(self):
        item = provider("custom", option_fields={"width": "dimension", "height": "dimension"})
        with self.assertRaisesRegex(ProviderError, "width.*height"):
            await item.generate("portrait", "")
        item._post.assert_not_called()

    def test_mapping_values_must_be_field_paths(self):
        for path in (True, 1, [], {}, "params..width"):
            with self.subTest(path=path), self.assertRaises(ProviderError):
                provider("custom", option_fields={"width": path}).validate_config()
        provider("custom", option_fields={"width": "params.width", "height": "params.height", "seed": None, "scale": ""}).validate_config()

    async def test_openai_summary_uses_actual_adapted_and_merged_parameters(self):
        item = provider(timeout=320, extra_body={"quality": "low", "output_format": "webp"})
        await item.generate("portrait", "abs", options={"width": 832, "height": 1216, "steps": 28, "scale": 5, "seed": -1})
        self.assertEqual(item.last_request["options"], {"size": "1024x1536", "quality": "low", "output_format": "webp", "width": 1024, "height": 1536})
        self.assertEqual(item.last_request["provider_timeout_sec"], 320)
        self.assertEqual(item.last_request["reference_count"], 0)

    async def test_gemini_summary_uses_extra_body_image_config(self):
        item = provider("gemini", extra_body={"generationConfig": {"imageConfig": {"aspectRatio": "16:9", "imageSize": "2K"}}})
        await item.generate("portrait", "", options={"aspect_ratio": "1:1"})
        self.assertEqual(item.last_request["options"], {"aspect_ratio": "16:9", "image_size": "2K"})

    async def test_novelai_summary_uses_real_sampling_parameters_without_reference_payload(self):
        item = provider("novelai", supports_image_edit=True, extra_body={"parameters": {"scale": 7, "steps": 30}})
        await item.generate("portrait", "abs", reference=JPEG, options={"width": 512, "height": 512, "seed": 12})
        self.assertEqual(item.last_request["options"]["scale"], 7)
        self.assertEqual(item.last_request["options"]["steps"], 30)
        self.assertEqual(item.last_request["options"]["seed"], 12)
        self.assertEqual(item.last_request["reference_count"], 1)
        self.assertNotIn("image", item.last_request["options"])

    async def test_custom_summary_uses_effective_mapped_values_and_never_credentials(self):
        item = provider("custom", api_key="private-test-key", supports_image_edit=True, negative_prompt=True, prompt_field="request.text", model_field="engine", negative_prompt_field="request.negative", reference_field="request.reference", option_fields={"width": "request.width", "height": "request.height"}, extra_body={"request": {"width": 1024, "token": "another-private-token"}})
        await item.generate("portrait private-test-key", "watermark", reference=JPEG, options={"width": 512, "height": 768})
        summary = item.last_request
        self.assertEqual(summary["options"], {"width": 1024, "height": 768})
        self.assertEqual(summary["prompt"], "portrait [REDACTED]")
        self.assertEqual(summary["negative_prompt"], "watermark")
        self.assertEqual(summary["reference_count"], 1)
        serialized = json.dumps(summary)
        for forbidden in ("private-test-key", "another-private-token", "Bearer", "data:image", base64.b64encode(JPEG).decode()):
            self.assertNotIn(forbidden, serialized)

    async def test_transport_failure_preserves_current_summary_and_validation_clears_old_one(self):
        item = provider(negative_mode="field")
        await item.generate("first portrait", "first negative")
        item._post.side_effect = ProviderError("offline transport failure")
        with self.assertRaises(ProviderError):
            await item.generate("second portrait", "second negative")
        self.assertEqual(item.last_request["prompt"], "second portrait")
        self.assertEqual(item.last_request["negative_prompt"], "second negative")
        with self.assertRaises(ProviderError):
            await item.generate("", "third negative")
        self.assertEqual(item.last_request, {})


if __name__ == "__main__":
    unittest.main()
