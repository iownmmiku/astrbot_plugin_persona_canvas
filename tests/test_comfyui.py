"""ComfyUI contracts with fully simulated HTTP and no image-generation service."""
import asyncio
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1]))
from providers.base import ImageProvider, MAX_IMAGE_BYTES, ProviderError, provider_from_config
from providers.comfyui import ComfyUIProvider
from storage import DEFAULT_SETTINGS


def image_bytes(fmt="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(buffer, format=fmt)
    return buffer.getvalue()


PNG = image_bytes()
JPEG = image_bytes("JPEG")
WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 11, "steps": 20, "cfg": 7.0, "sampler_name": "euler", "positive": ["6", 0], "negative": ["7", 0]}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 768, "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "original positive", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "original negative", "clip": ["4", 1]}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "existing-checkpoint.safetensors"}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["8", 0]}},
    "10": {"class_type": "LoadImage", "inputs": {"image": "existing-reference.png"}},
}
FIELDS = {
    "prompt": "6.inputs.text", "negative_prompt": "7.inputs.text",
    "seed": "3.inputs.seed", "steps": "3.inputs.steps", "scale": "3.inputs.cfg",
    "sampler": "3.inputs.sampler_name", "width": "5.inputs.width", "height": "5.inputs.height",
}


def provider(**overrides):
    config = {"kind": "comfyui", "endpoint": "http://127.0.0.1:8188", "workflow": copy.deepcopy(WORKFLOW), "workflow_fields": copy.deepcopy(FIELDS), "model": "portrait workflow", **overrides}
    item = provider_from_config("local", config)
    item.poll_interval = 0.001
    return item


def json_reply(value):
    return json.dumps(value).encode(), "application/json", 200


def history(prompt_id="task-123", **overrides):
    return {prompt_id: {"status": {"status_str": "success", "completed": True, "messages": []}, "outputs": {"9": {"images": [{"filename": "photo.png", "subfolder": "portraits", "type": "output"}]}}, **overrides}}


def working_http(item, *, result_history=None, view=PNG, mime="image/png"):
    async def request(method, url, body=None, **kwargs):
        if url.endswith("/prompt"):
            return json_reply({"prompt_id": "task-123", "number": 0, "node_errors": {}})
        if "/history/" in url:
            return json_reply(result_history if result_history is not None else history())
        if url.endswith("/upload/image"):
            return json_reply({"name": "reference-0.jpg", "subfolder": "persona_canvas", "type": "input"})
        if "/view?" in url:
            return view, mime, 200
        if url.endswith("/queue"):
            return b"", "", 200
        if url.endswith("/object_info"):
            return json_reply({node["class_type"]: {} for node in WORKFLOW.values()})
        raise AssertionError(f"Unexpected HTTP {method} {url}")
    item._http = AsyncMock(side_effect=request)
    return item._http


class ComfyUIContracts(unittest.IsolatedAsyncioTestCase):
    def test_factory_capabilities_and_local_addresses(self):
        for endpoint in ("http://127.0.0.1:8188", "http://192.168.1.50:8188", "http://[::1]:8188", "http://localhost:8188/api"):
            item = provider(endpoint=endpoint)
            item.validate_config()
            self.assertIsInstance(item, ComfyUIProvider)
            self.assertTrue(item.capabilities.seed)
            self.assertTrue(item.capabilities.negative_prompt)
            self.assertFalse(item.capabilities.image_to_image)
        direct = ImageProvider("direct", {"kind": "comfyui", "workflow_fields": {"prompt": "6.inputs.text", "reference": "10.inputs.image"}})
        self.assertIsInstance(direct, ComfyUIProvider)
        self.assertTrue(direct.capabilities.image_to_image)
        self.assertEqual(direct.capabilities.max_reference_images, 1)

    def test_configuration_rejects_frontend_json_broken_paths_links_and_conflicts(self):
        invalid = [
            {"workflow": {"nodes": [], "links": []}},
            {"workflow": {}}, {"workflow_fields": {}},
            {"workflow_fields": {"prompt": "6.inputs.missing"}},
            {"workflow_fields": {"prompt": "99.inputs.text"}},
            {"workflow_fields": {"prompt": "6.inputs.text.extra"}},
            {"workflow_fields": {"prompt": "6.inputs.clip"}},
            {"workflow_fields": {"prompt": "6.inputs.text", "negative_prompt": "6.inputs.text"}},
            {"workflow_fields": {"prompt": "6.inputs.text", "reference": "3.inputs.seed"}},
            {"workflow_fields": {"prompt": "6.inputs.text", "seed": "7.inputs.text"}},
            {"workflow_fields": {"prompt": "6.inputs.text", "unknown": "3.inputs.seed"}},
            {"workflow_fields": {"prompt": "6.inputs.text", "seed": []}},
            {"output_node": "missing"}, {"output_node": 9},
            {"extra_body": {"prompt": {}}},
            {"endpoint": "http://user:secret@localhost:8188"},
            {"endpoint": "http://localhost:8188?token=secret"},
            {"endpoint": "http://localhost:invalid"},
            {"workflow": {"1": {"inputs": {}}}},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                provider(**changes).validate_config()

    async def test_generation_maps_scalars_preserves_graph_and_negative_channel(self):
        item = provider()
        calls = working_http(item)
        result = await item.generate("a seated girl", "abs, muscular female", options={"seed": 19, "steps": 30, "scale": 4.5, "width": 832, "height": 1216, "sampler": "dpmpp_2m", "_explicit": ["seed", "steps", "scale", "width", "height", "sampler"]})
        body = next(call.args[2] for call in calls.call_args_list if call.args[1].endswith("/prompt"))
        actual = body["prompt"]
        self.assertEqual(actual["6"]["inputs"]["text"], "a seated girl")
        self.assertEqual(actual["7"]["inputs"]["text"], "abs, muscular female")
        self.assertNotIn("muscular", actual["6"]["inputs"]["text"])
        self.assertEqual(actual["3"]["inputs"], {"seed": 19, "steps": 30, "cfg": 4.5, "sampler_name": "dpmpp_2m", "positive": ["6", 0], "negative": ["7", 0]})
        self.assertEqual(actual["4"], WORKFLOW["4"])
        self.assertNotIn("model", body)
        self.assertEqual(item.config["workflow"], WORKFLOW)
        self.assertEqual((result.data, result.extension, result.provider, result.model), (PNG, "png", "local", "portrait workflow"))
        self.assertTrue(item.last_request["submitted"])
        self.assertEqual(item.last_request["workflow_prompt_id"], "task-123")
        self.assertEqual(item.last_request["negative_prompt_field"], "7.inputs.text")
        self.assertEqual(item.last_request["negative_source"], "plugin")
        self.assertEqual(item.last_request["options"]["seed"], 19)
        self.assertNotIn("workflow", item.last_request)
        view = next(call for call in calls.call_args_list if "/view?" in call.args[1])
        self.assertEqual(view.args[1], "http://127.0.0.1:8188/view?filename=photo.png&subfolder=portraits&type=output")
        self.assertEqual(view.kwargs["limit"], MAX_IMAGE_BYTES)

    async def test_unmapped_shared_defaults_stay_in_workflow_explicit_values_fail(self):
        item = provider(workflow_fields={"prompt": FIELDS["prompt"]}, negative_mode="natural_language")
        calls = working_http(item)
        await item.generate("flower", "watermark", options={"width": 832, "height": 1216, "seed": 5, "steps": 28, "scale": 5})
        actual = next(call.args[2]["prompt"] for call in calls.call_args_list if call.args[1].endswith("/prompt"))
        self.assertEqual(actual["3"], WORKFLOW["3"])
        self.assertEqual(actual["5"], WORKFLOW["5"])
        self.assertEqual(actual["7"], WORKFLOW["7"])
        self.assertEqual(actual["6"]["inputs"]["text"], "flower")
        self.assertEqual(item.last_request["negative_mode"], "disabled")
        self.assertEqual(item.last_request["negative_prompt"], "")
        for key, value in (("seed", 5), ("steps", 20), ("scale", 5), ("sampler", "euler"), ("width", 768), ("strength", .5)):
            calls.reset_mock()
            with self.subTest(key=key), self.assertRaises(ProviderError):
                await item.generate("flower", "", options={key: value, "_explicit": [key]})
            calls.assert_not_called()

    async def test_actual_shared_generation_defaults_use_comfy_sampler_name(self):
        item = provider()
        calls = working_http(item)
        options = copy.deepcopy(DEFAULT_SETTINGS["generation"])
        with patch("providers.comfyui.secrets.randbits", return_value=123456):
            await item.generate("a seated girl", "watermark", options=options)
        actual = next(call.args[2]["prompt"] for call in calls.call_args_list if call.args[1].endswith("/prompt"))
        self.assertEqual(actual["3"]["inputs"]["sampler_name"], "euler_ancestral")
        self.assertEqual(actual["3"]["inputs"]["steps"], DEFAULT_SETTINGS["generation"]["steps"])
        self.assertEqual(actual["3"]["inputs"]["cfg"], DEFAULT_SETTINGS["generation"]["scale"])
        self.assertEqual(actual["3"]["inputs"]["seed"], 123456)
        self.assertEqual(actual["5"]["inputs"]["width"], DEFAULT_SETTINGS["generation"]["width"])
        self.assertEqual(actual["5"]["inputs"]["height"], DEFAULT_SETTINGS["generation"]["height"])
        self.assertEqual(item.last_request["options"]["sampler"], "euler_ancestral")
        self.assertTrue(any("适配" in note for note in item.last_request["notes"]))
        self.assertEqual(options, DEFAULT_SETTINGS["generation"])
        self.assertEqual(item.config["workflow"], WORKFLOW)

    async def test_only_known_nonexplicit_sampler_aliases_are_adapted(self):
        item = provider()
        calls = working_http(item)
        for name in ("euler", "euler_ancestral", "dpm_2", "dpm_2_ancestral", "lms", "heun", "dpmpp_2m"):
            with self.subTest(sampler=name):
                await item.generate("flower", "", options={"sampler": "k_" + name})
                self.assertEqual(item.last_request["options"]["sampler"], name)
        for name in ("k_euler_ancestral", "k_custom_sampler", "custom_sampler"):
            with self.subTest(explicit_sampler=name):
                await item.generate("flower", "", options={"sampler": name, "_explicit": ["sampler"]})
                self.assertEqual(item.last_request["options"]["sampler"], name)
                self.assertFalse(any("适配" in note for note in item.last_request["notes"]))
        calls.reset_mock()
        await item.generate("flower", "", options={"sampler": "k_unknown_sampler"})
        actual = next(call.args[2]["prompt"] for call in calls.call_args_list if call.args[1].endswith("/prompt"))
        self.assertEqual(actual["3"]["inputs"]["sampler_name"], WORKFLOW["3"]["inputs"]["sampler_name"])
        self.assertEqual(item.last_request["options"]["sampler"], "euler")
        self.assertTrue(any("沿用工作流采样器" in note for note in item.last_request["notes"]))

    async def test_uint64_and_random_seed_are_valid_actual_values(self):
        item = provider()
        calls = working_http(item)
        for value in (0, 2 ** 64 - 1, str(2 ** 64 - 1)):
            await item.generate("flower", "", options={"seed": value, "_explicit": ["seed"]})
            self.assertEqual(item.last_request["options"]["seed"], int(value))
        with patch("providers.comfyui.secrets.randbits", return_value=123456):
            await item.generate("flower", "", options={"seed": -1})
        self.assertEqual(item.last_request["options"]["seed"], 123456)
        for value in (True, 2 ** 64, -2, 1.2):
            calls.reset_mock()
            with self.subTest(value=value), self.assertRaises(ProviderError):
                await item.generate("flower", "", options={"seed": value})
            calls.assert_not_called()

    async def test_reference_upload_uses_image_field_and_returned_input_filename(self):
        item = provider(workflow_fields={**FIELDS, "reference": "10.inputs.image"})
        calls = working_http(item)
        await item.generate("same girl", "", reference=JPEG)
        upload = calls.call_args_list[0]
        self.assertEqual(upload.args[:2], ("POST", "http://127.0.0.1:8188/upload/image"))
        self.assertEqual(upload.args[2], {"type": "input", "subfolder": "persona_canvas", "overwrite": "false"})
        self.assertEqual(upload.kwargs["files"], {"image": JPEG})
        actual = next(call.args[2]["prompt"] for call in calls.call_args_list if call.args[1].endswith("/prompt"))
        self.assertEqual(actual["10"]["inputs"]["image"], "persona_canvas/reference-0.jpg")
        self.assertEqual(item.config["workflow"]["10"], WORKFLOW["10"])
        self.assertEqual(item.last_request["reference_count"], 1)

    async def test_reference_missing_mapping_multiple_images_or_bad_pixels_fail_before_io(self):
        for item, reference in ((provider(), JPEG), (provider(workflow_fields={**FIELDS, "reference": "10.inputs.image"}), [PNG, JPEG]), (provider(workflow_fields={**FIELDS, "reference": "10.inputs.image"}), b"fake image")):
            calls = working_http(item)
            with self.assertRaises(ProviderError):
                await item.generate("flower", "", reference=reference)
            calls.assert_not_called()

    async def test_upload_failure_is_not_reported_as_generation_submitted(self):
        item = provider(workflow_fields={**FIELDS, "reference": "10.inputs.image"})
        item._http = AsyncMock(return_value=json_reply({"name": "../../secret.png", "type": "input"}))
        with self.assertRaises(ProviderError):
            await item.generate("flower", "", reference=PNG)
        self.assertFalse(item.last_request["submitted"])
        self.assertEqual(item._http.call_count, 1)

    async def test_history_failure_has_no_sensitive_echo_or_image_download(self):
        for payload in (
            history(status={"status_str": "error", "completed": False, "messages": [["execution_error", {"exception_message": "private-workflow-secret"}]]}),
            history(status={"status_str": "success", "completed": True}, outputs={}),
            {"task-123": None},
            {"task-123": "not a history"},
            history(status="not status"),
        ):
            if payload == {"task-123": None}:
                continue  # Missing history means the queued task has not finished.
            item = provider()
            calls = working_http(item, result_history=payload)
            with self.subTest(payload=payload), self.assertRaises(ProviderError) as error:
                await item.generate("flower", "")
            self.assertNotIn("private-workflow-secret", str(error.exception))
            self.assertFalse(any("/view?" in call.args[1] for call in calls.call_args_list))

    async def test_output_node_selects_requested_image(self):
        result_history = history(outputs={"other": {"images": [{"filename": "wrong.png", "type": "output"}]}, "9": {"images": [{"filename": "right.png", "type": "temp"}]}})
        item = provider(output_node="9")
        calls = working_http(item, result_history=result_history)
        await item.generate("flower", "")
        url = next(call.args[1] for call in calls.call_args_list if "/view?" in call.args[1])
        self.assertIn("filename=right.png", url)
        self.assertIn("type=temp", url)

    async def test_legacy_history_without_status_still_accepts_valid_images(self):
        item = provider()
        working_http(item, result_history=history(status=None))
        result = await item.generate("flower", "")
        self.assertEqual(result.data, PNG)

    async def test_mapped_pixel_limit_rejects_before_submitting_work(self):
        item = provider()
        calls = working_http(item)
        with self.assertRaisesRegex(ProviderError, "像素"):
            await item.generate("flower", "", options={"width": 8192, "height": 8192})
        calls.assert_not_called()

    async def test_output_paths_urls_mime_and_pixels_are_verified(self):
        invalid_files = [
            {"filename": "https://evil.example/image.png"},
            {"filename": "../private.png"},
            {"filename": "image.png", "subfolder": "../private"},
            {"filename": "image.png", "subfolder": "C:\\private"},
            {"filename": "image.png", "type": "input"},
        ]
        for descriptor in invalid_files:
            item = provider()
            calls = working_http(item, result_history=history(outputs={"9": {"images": [descriptor]}}))
            with self.subTest(descriptor=descriptor), self.assertRaises(ProviderError):
                await item.generate("flower", "")
            self.assertFalse(any("/view?" in call.args[1] for call in calls.call_args_list))
        for data, mime in ((b"fake", "image/png"), (JPEG, "image/png"), (PNG, "text/html")):
            item = provider()
            working_http(item, view=data, mime=mime)
            with self.subTest(mime=mime), self.assertRaises(ProviderError):
                await item.generate("flower", "")

    async def test_timeout_removes_only_own_queued_prompt(self):
        item = provider()
        calls = working_http(item, result_history={})
        original_wait = asyncio.wait_for
        async def short_wait(coro, timeout):
            return await original_wait(coro, min(timeout, .02))
        with patch("providers.comfyui.asyncio.wait_for", side_effect=short_wait):
            with self.assertRaisesRegex(ProviderError, "等待超时"):
                await item.generate("flower", "")
        cleanup = calls.call_args_list[-1]
        self.assertEqual(cleanup.args, ("POST", "http://127.0.0.1:8188/queue", {"delete": ["task-123"]}))
        self.assertFalse(any("interrupt" in call.args[1] for call in calls.call_args_list))

    async def test_cancel_removes_own_queue_only_and_propagates_cancellation(self):
        item = provider()
        calls = working_http(item, result_history={})
        task = asyncio.create_task(item.generate("flower", ""))
        for _ in range(50):
            if item.last_request.get("workflow_prompt_id"):
                break
            await asyncio.sleep(.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(calls.call_args_list[-1].args[2], {"delete": ["task-123"]})
        self.assertTrue(calls.call_args_list[-1].args[1].endswith("/queue"))

    async def test_queue_cleanup_failure_keeps_original_timeout(self):
        item = provider()
        http = working_http(item, result_history={})
        existing = http.side_effect
        async def failed_cleanup(method, url, body=None, **kwargs):
            if url.endswith("/queue"):
                raise ProviderError("queue unreachable")
            return await existing(method, url, body, **kwargs)
        http.side_effect = failed_cleanup
        original_wait = asyncio.wait_for
        async def short_wait(coro, timeout):
            return await original_wait(coro, min(timeout, .01))
        with patch("providers.comfyui.asyncio.wait_for", side_effect=short_wait):
            with self.assertRaisesRegex(ProviderError, "等待超时"):
                await item.generate("flower", "")

    async def test_submission_json_errors_and_missing_id_do_not_leak_workflow(self):
        for response in (b"not JSON", json.dumps({"error": {"message": "workflow private-secret"}}).encode(), json.dumps({"prompt_id": "http://evil"}).encode(), b"[]"):
            item = provider()
            item._http = AsyncMock(return_value=(response, "application/json", 200))
            with self.subTest(response=response), self.assertRaises(ProviderError) as error:
                await item.generate("flower", "")
            self.assertNotIn("private-secret", str(error.exception))
            self.assertEqual(item._http.call_count, 1)

    async def test_read_only_connection_checks_nodes_and_workflow_label_is_not_checkpoint(self):
        item = provider()
        calls = working_http(item)
        result = await item.test_connection()
        self.assertTrue(result["ok"])
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(calls.call_args.args[:2], ("GET", "http://127.0.0.1:8188/object_info"))
        models = await item.list_models()
        self.assertEqual(models["models"], ["portrait workflow"])
        self.assertFalse(models["automatic"])
        item._http = AsyncMock(return_value=json_reply({"CLIPTextEncode": {}}))
        result = await item.test_connection()
        self.assertFalse(result["ok"])
        self.assertIn("缺少", result["message"])

    async def test_transport_upload_mime_fixed_origin_redirect_policy_and_json_body(self):
        captures = []
        class Reply:
            status = 200
            def __init__(self, raw, mime):
                self.raw, self.headers, self.content = raw, {"Content-Type": mime}, self
            async def iter_chunked(self, size):
                yield self.raw
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
            def request(self, method, url, **kwargs):
                captures.append((method, url, kwargs))
                if url.endswith("/upload/image"):
                    raw, mime, _ = json_reply({"name": "reference.jpg", "subfolder": "persona_canvas", "type": "input"})
                elif url.endswith("/prompt"):
                    raw, mime, _ = json_reply({"prompt_id": "task-123"})
                elif "/history/" in url:
                    raw, mime, _ = json_reply(history())
                else:
                    raw, mime = PNG, "image/png"
                return Reply(raw, mime)
        item = provider(endpoint="http://192.168.1.50:8188/api", workflow_fields={**FIELDS, "reference": "10.inputs.image"})
        with patch("aiohttp.ClientSession", Session):
            result = await item.generate("portrait", "", reference=JPEG)
        self.assertEqual(result.data, PNG)
        self.assertEqual(len(captures), 4)
        self.assertTrue(all(url.startswith("http://192.168.1.50:8188/api/") for _, url, _ in captures))
        self.assertTrue(all(not values["allow_redirects"] for _, _, values in captures))
        upload = captures[0][2]
        self.assertNotIn("Content-Type", upload["headers"])
        actual_files = [(meta.get("name"), headers.get("Content-Type"), data) for meta, headers, data in upload["data"]._fields if headers.get("Content-Type", "").startswith("image/")]
        self.assertEqual(actual_files, [("image", "image/jpeg", JPEG)])
        submitted = json.loads(captures[1][2]["data"])
        self.assertEqual(submitted["prompt"]["10"]["inputs"]["image"], "persona_canvas/reference.jpg")
        self.assertEqual(captures[1][2]["headers"]["Content-Type"], "application/json")

    async def test_transport_error_body_never_echoes_embedded_workflow_credentials(self):
        workflow = copy.deepcopy(WORKFLOW)
        workflow["10"]["inputs"]["api_key"] = "private-workflow-secret"
        class Reply:
            status = 400
            headers = {"Content-Type": "application/json"}
            def __init__(self):
                self.content = self
            async def iter_chunked(self, size):
                yield json.dumps({"error": {"message": "private-workflow-secret"}, "workflow": workflow}).encode()
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
                return Reply()
        item = provider(workflow=workflow)
        with patch("aiohttp.ClientSession", Session):
            with self.assertRaises(ProviderError) as error:
                await item.generate("portrait", "")
        self.assertNotIn("private-workflow-secret", str(error.exception))
        self.assertNotIn("private-workflow-secret", json.dumps(item.last_request))
        self.assertIn("HTTP 400", str(error.exception))

    async def test_cancel_before_prompt_response_never_deletes_unowned_queue_items(self):
        item = provider()
        entered = asyncio.Event()
        async def hang(method, url, body=None, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        item._http = AsyncMock(side_effect=hang)
        task = asyncio.create_task(item.generate("portrait", ""))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(item._http.call_count, 1)
        self.assertTrue(item._http.call_args.args[1].endswith("/prompt"))


if __name__ == "__main__":
    unittest.main()
