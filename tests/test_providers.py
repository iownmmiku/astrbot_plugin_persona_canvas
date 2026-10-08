"""Offline contracts for native image requests, decoding, and transport bounds."""
import base64
import io
import json
import sys
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1]))
from providers.base import (
    CustomProvider, GeminiProvider, NovelAIProvider, OpenAIProvider,
    ProviderError, ImageProvider, _image_bytes, _image_info, provider_from_config,
)


def image_bytes(fmt="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(output, format=fmt)
    return output.getvalue()


PNG = image_bytes()
JPEG = image_bytes("JPEG")
WEBP = image_bytes("WEBP")


def encoded(data=PNG):
    return base64.b64encode(data).decode("ascii")


def response(data=PNG):
    return json.dumps({"data": [{"b64_json": encoded(data)}]}).encode(), "application/json"


def provider(kind="openai", **config):
    value = provider_from_config("drawing", {"kind": kind, "endpoint": "https://provider.example/v1", "model": "gpt-image-1.5", **config})
    value._post = AsyncMock(return_value=response())
    return value


class ProviderContracts(unittest.IsolatedAsyncioTestCase):
    def test_distinct_factories_and_capabilities(self):
        for kind, cls in [("openai", OpenAIProvider), ("gemini", GeminiProvider), ("novelai", NovelAIProvider), ("custom", CustomProvider)]:
            item = provider(kind)
            self.assertIsInstance(item, cls)
            self.assertIn("identity_reference", asdict(item.capabilities))
        with self.assertRaises(ProviderError):
            provider("misspelled")
        self.assertIsInstance(ImageProvider("direct", {"kind": "novelai"}), NovelAIProvider)
        self.assertTrue(ImageProvider("direct", {"kind": "novelai"}).capabilities.seed)

    def test_real_mime_and_pixel_validation(self):
        for data, ext, mime in [(PNG, "png", "image/png"), (JPEG, "jpg", "image/jpeg"), (WEBP, "webp", "image/webp")]:
            self.assertEqual(_image_info(data), (ext, mime, (64, 64)))
            self.assertEqual(_image_bytes(f"data:{mime};base64,{encoded(data)}"), (data, ext))
        for value in ["", "garbage!", encoded(b"not pixels"), f"data:image/png;base64,{encoded(JPEG)}", encoded(PNG[:30])]:
            with self.subTest(value=value[:20]), self.assertRaises(ProviderError):
                _image_bytes(value)

    async def test_gpt_native_request_adapts_defaults_without_implicit_negative(self):
        item = provider(extra_body={"quality": "low", "output_format": "webp"})
        result = await item.generate("a blue flower", "watermark", options={"width": 832, "height": 1216, "steps": 28, "scale": 5, "seed": -1})
        args = item._post.call_args.args
        self.assertEqual(args[0], "https://provider.example/v1/images/generations")
        body = args[1]
        self.assertNotIn("response_format", body)
        self.assertEqual(body["size"], "1024x1536")
        self.assertEqual(body["quality"], "low")
        self.assertNotIn("watermark", body["prompt"])
        self.assertNotIn("negative_prompt", body)
        self.assertEqual(item.capabilities.negative_mode, "disabled")
        self.assertEqual((result.data, result.extension, result.provider, result.model), (PNG, "png", "drawing", "gpt-image-1.5"))
        self.assertFalse(item.capabilities.negative_prompt)

    async def test_explicit_unsupported_values_fail_before_call(self):
        for options in [{"seed": 5}, {"sampler": "custom", "_explicit": ["sampler"]}, {"strength": 0.5, "_explicit": ["strength"]}, {"width": 832, "height": 1216, "_explicit": ["width", "height"]}]:
            item = provider()
            with self.subTest(options=options), self.assertRaises(ProviderError):
                await item.generate("flower", "", options=options)
            item._post.assert_not_called()

    async def test_supported_native_sizes_and_legacy_b64(self):
        item = provider(model="gpt-image-2", endpoint="https://provider.example")
        await item.generate("flower", "", options={"width": 1536, "height": 864, "_explicit": ["width", "height"]})
        self.assertEqual(item._post.call_args.args[1]["size"], "1536x864")
        await item.generate("flower", "", options={"size": "1536x864", "_explicit": ["size"]})
        self.assertEqual(item._post.call_args.args[1]["size"], "1536x864")
        old = provider(model="dall-e-3")
        await old.generate("flower", "", options={"width": 832, "height": 1216})
        self.assertEqual(old._post.call_args.args[1]["response_format"], "b64_json")
        self.assertEqual(old._post.call_args.args[1]["size"], "1024x1792")

    def test_blank_console_routes_keep_native_defaults(self):
        for kind, default in [("openai", "/v1/images/generations"), ("gemini", "/models/gemini-image:generateContent"), ("novelai", "/ai/generate-image"), ("custom", "")]:
            item = provider(kind, endpoint="https://provider.example", generation_path="", edit_path=" ", models_path="")
            self.assertEqual(item._url("generation_path", default), "https://provider.example" + default)
            self.assertEqual(item._url("models_path", "/v1/models"), "https://provider.example/v1/models")

    def test_gemini_native_auth_and_explicit_proxy_override(self):
        for auth in [{}, {"auth_header": "", "auth_prefix": ""}]:
            item = provider("gemini", api_key="test-key", **auth)
            self.assertEqual(item._headers()["x-goog-api-key"], "test-key")
            self.assertNotIn("Authorization", item._headers())
        proxy = provider("gemini", api_key="test-key", auth_header="Authorization", auth_prefix="Bearer ")
        self.assertEqual(proxy._headers()["Authorization"], "Bearer test-key")
        native = provider("gemini", api_key="test-key", auth_header="x-goog-api-key", auth_prefix="")
        self.assertEqual(native._headers()["x-goog-api-key"], "test-key")

    async def test_reference_is_never_silently_dropped(self):
        for kind in ["openai", "gemini", "novelai", "custom"]:
            item = provider(kind)
            with self.subTest(kind=kind), self.assertRaises(ProviderError):
                await item.generate("change coat", "", reference=JPEG)
            item._post.assert_not_called()
        item = provider(supports_image_edit=True)
        await item.generate("change coat", "", reference=JPEG)
        self.assertEqual(item._post.call_args.args[0], "https://provider.example/v1/images/edits")
        self.assertEqual(item._post.call_args.kwargs["files"], {"image": JPEG})

    async def test_extra_body_cannot_change_prompt_or_image(self):
        for kind, extra in [("openai", {"prompt": "other"}), ("openai", {"image": "other"}), ("gemini", {"contents": []}), ("novelai", {"parameters": {"v4_prompt": {"caption": {"base_caption": "other"}}}})]:
            item = provider(kind, model="nai-diffusion-4-5-full" if kind == "novelai" else "model", extra_body=extra)
            with self.subTest(kind=kind), self.assertRaises(ProviderError):
                await item.generate("user prompt", "")
            item._post.assert_not_called()
        item = provider(extra_body={"response_format": "b64_json"})
        with self.assertRaises(ProviderError):
            await item.generate("flower", "")

    async def test_gemini_reference_mime_and_final_image_selection(self):
        item = provider("gemini", model="models/gemini-image", endpoint="https://google.example/v1beta", supports_image_edit=True, api_key="secret")
        payload = {"candidates": [{"content": {"parts": [{"thought": True, "inlineData": {"mimeType": "image/png", "data": encoded()}}, {"text": "Here is the image"}, {"inlineData": {"mimeType": "image/jpeg", "data": encoded(JPEG)}}]}}]}
        item._post.return_value = json.dumps(payload).encode(), "application/json"
        result = await item.generate("change coat", "logo", reference=JPEG, options={"aspect_ratio": "3:4"})
        body = item._post.call_args.args[1]
        self.assertTrue(item._post.call_args.args[0].endswith("/models/gemini-image:generateContent"))
        self.assertEqual(body["contents"][0]["parts"][1]["inlineData"]["mimeType"], "image/jpeg")
        self.assertEqual(body["generationConfig"]["imageConfig"]["aspectRatio"], "3:4")
        self.assertNotIn("logo", body["contents"][0]["parts"][0]["text"])
        self.assertEqual(result.extension, "jpg")
        self.assertEqual(item._headers()["x-goog-api-key"], "secret")
        with self.assertRaises(ProviderError):
            await item.generate("flower", "", options={"width": 832, "aspect_ratio": "3:4", "_explicit": ["width", "aspect_ratio"]})
        item._post.return_value = b'{"candidates": []}', "application/json"
        with self.assertRaises(ProviderError):
            await item.generate("flower", "")

    async def test_novelai_real_img2img_and_zip(self):
        item = provider("novelai", model="nai-diffusion-4-5-full", endpoint="https://nai.example", supports_image_edit=True, extra_body={"parameters": {"cfg_rescale": 0.2}})
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("image_0.png", PNG)
        item._post.return_value = output.getvalue(), "application/zip"
        result = await item.generate("new coat", "logo", reference=JPEG, options={"width": 512, "height": 512, "seed": -1, "strength": 0.4, "noise": 0.1})
        body = item._post.call_args.args[1]
        self.assertEqual(body["action"], "img2img")
        params = body["parameters"]
        self.assertEqual(params["strength"], 0.4)
        self.assertEqual(params["noise"], 0.1)
        self.assertEqual(_image_info(base64.b64decode(params["image"]))[1], "image/png")
        self.assertGreaterEqual(params["seed"], 0)
        self.assertEqual(params["cfg_rescale"], 0.2)
        self.assertEqual(params["v4_prompt"]["caption"]["base_caption"], "new coat")
        self.assertEqual(params["v4_negative_prompt"]["caption"]["base_caption"], "logo")
        self.assertEqual(result.data, PNG)
        self.assertFalse(item.capabilities.automated_generation)
        item._post.return_value = json.dumps({"images": [{"image": encoded(WEBP)}]}).encode(), "application/json"
        self.assertEqual((await item.generate("flower", "")).extension, "webp")

    async def test_custom_nested_routes_and_response(self):
        item = provider("custom", endpoint="https://custom.example/api", generation_path="draw", edit_path="edit", prompt_field="request.text", negative_prompt=True, negative_prompt_field="request.negative", reference_field="request.reference", supports_image_edit=True, option_fields={"width": "request.width"}, response_path="result.images.0", extra_body={"request": {"quality": "draft"}})
        item._post.return_value = json.dumps({"result": {"images": [encoded()]}}).encode(), "application/json"
        await item.generate("coat", "logo", reference=JPEG, options={"width": 512})
        url, body = item._post.call_args.args
        self.assertEqual(url, "https://custom.example/api/edit")
        self.assertEqual(body["request"]["text"], "coat")
        self.assertEqual(body["request"]["negative"], "logo")
        self.assertTrue(body["request"]["reference"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(body["request"]["quality"], "draft")
        self.assertEqual(body["request"]["width"], 512)

    async def test_custom_field_collisions_cannot_replace_user_content(self):
        for config in [{"model_field": "prompt"}, {"reference_field": "prompt", "supports_image_edit": True}, {"prompt_field": "request.text", "negative_prompt": True, "negative_prompt_field": "request"}]:
            item = provider("custom", **config)
            with self.subTest(config=config), self.assertRaises(ProviderError):
                await item.generate("user prompt", "negative", reference=JPEG if config.get("supports_image_edit") else None)
            item._post.assert_not_called()

    async def test_empty_malformed_error_and_false_content_types(self):
        item = provider()
        for raw, mime in [(b'', "application/json"), (b'{"data": []}', "application/json"), (b'{"data": [null]}', "application/json"), (b'{"error": {"message": "secret"}}', "application/json"), (b'not an image', "image/png")]:
            item._post.return_value = raw, mime
            with self.subTest(raw=raw), self.assertRaises(ProviderError):
                await item.generate("flower", "")
        item._post.return_value = JPEG, "image/png"
        self.assertEqual((await item.generate("flower", "")).extension, "jpg")

    async def test_image_url_opt_in_domain_dns_and_private_boundaries(self):
        item = provider(api_key="secret")
        item._resolve_host = AsyncMock(return_value=["93.184.216.34"])
        item._download = AsyncMock(return_value=PNG)
        with self.assertRaises(ProviderError):
            await item._download_image("https://provider.example/picture")
        item.config["allow_image_urls"] = True
        self.assertEqual(await item._download_image("https://provider.example/picture"), PNG)
        item._download.assert_awaited_with("https://provider.example/picture", ["93.184.216.34"])
        for url in ["https://other.example/x", "https://secret@provider.example/x", "file:///secret"]:
            with self.subTest(url=url), self.assertRaises(ProviderError):
                await item._download_image(url)
        item.config["image_url_allowed_hosts"] = ["cdn.example"]
        item._resolve_host.return_value = ["127.0.0.1"]
        with self.assertRaises(ProviderError):
            await item._download_image("https://cdn.example/x")
        # An explicitly configured local provider may return its own local image.
        item.config["endpoint"] = "http://127.0.0.1:8188"
        self.assertEqual(await item._download_image("http://127.0.0.1:8188/image"), PNG)

    async def test_models_and_free_connection_are_not_generation(self):
        item = provider()
        item._request = AsyncMock(return_value=(b'{"data": [{"id": "b"}, {"id": "a"}]}', "application/json", 200))
        self.assertEqual((await item.list_models())["models"], ["a", "b"])
        self.assertTrue((await item.test_connection())["ok"])
        item._post.assert_not_called()
        nai = provider("novelai")
        result = await nai.test_connection()
        self.assertFalse(result["ok"])
        self.assertTrue(result["requires_generation_test"])
        nai._post.assert_not_called()
        item._request.side_effect = ProviderError("failure Bearer secret api_key=secret")
        item.config["api_key"] = "secret"
        self.assertNotIn("secret", (await item.test_connection())["message"])

    async def test_bounded_stream_does_not_read_entire_response(self):
        class Content:
            def __init__(self):
                self.count = 0
            async def iter_chunked(self, size):
                for chunk in [b"1234", b"5678", b"must not be read"]:
                    self.count += 1
                    yield chunk
        class Reply:
            headers = {}
            content = Content()
        reply = Reply()
        with self.assertRaises(ProviderError):
            await ImageProvider._read_response(reply, 5)
        self.assertEqual(reply.content.count, 2)

    async def test_multipart_uses_actual_mime_and_redacts_transport_errors(self):
        captures = {}
        class Content:
            async def iter_chunked(self, size):
                yield response()[0]
        class Reply:
            status = 200
            headers = {"Content-Type": "application/json"}
            content = Content()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        class Session:
            def __init__(self, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def request(self, *args, **kwargs):
                captures.update(kwargs)
                return Reply()
        item = provider(supports_image_edit=True)
        del item._post
        with patch("aiohttp.ClientSession", Session):
            await item.generate("coat", "", reference=JPEG)
        self.assertNotIn("Content-Type", captures["headers"])
        file_fields = captures["data"]._fields
        self.assertTrue(any(headers.get("Content-Type") == "image/jpeg" for _, headers, _ in file_fields))
        with patch("aiohttp.ClientSession", Session):
            await item.generate("coat", "", reference=[JPEG, WEBP])
        file_fields = captures["data"]._fields
        actual = [(meta.get("name"), headers.get("Content-Type"), data) for meta, headers, data in file_fields if headers.get("Content-Type", "").startswith("image/")]
        self.assertEqual(actual, [("image[]", "image/jpeg", JPEG), ("image[]", "image/webp", WEBP)])
        item.config["api_key"] = "secret"
        with patch("aiohttp.ClientSession", side_effect=OSError("network secret")):
            with self.assertRaises(ProviderError) as error:
                await item.generate("coat", "")
        self.assertNotIn("secret", str(error.exception))

    async def test_gemini_multiple_references_keep_real_mimes_and_enforce_limit(self):
        item = provider("gemini", model="gemini-2.5-flash-image", supports_image_edit=True)
        item._post.return_value = (json.dumps({"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": encoded()}}]}}]}).encode(), "application/json")
        await item.generate("same character", "", reference=[JPEG, WEBP])
        parts = item._post.call_args.args[1]["contents"][0]["parts"]
        self.assertEqual([part["inlineData"]["mimeType"] for part in parts if "inlineData" in part], ["image/jpeg", "image/webp"])
        item._post.reset_mock()
        with self.assertRaises(ProviderError):
            await item.generate("same character", "", reference=[PNG] * 4)
        item._post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
