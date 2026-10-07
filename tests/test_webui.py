"""Offline web transport tests; no AstrBot installation or provider IO required."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

ROOT = Path(__file__).parents[1]
package = types.ModuleType("canvas_web_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package


def load(name):
    fullname = f"canvas_web_test.{name}"
    spec = importlib.util.spec_from_file_location(fullname, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    spec.loader.exec_module(module)
    return module


page_api = load("page_api")
webui = load("webui_server")


class Plugin:
    def __init__(self, directory):
        self.storage = types.SimpleNamespace(assets=directory, settings={"providers": {"default": {"api_key": "test-secret", "auth_value": "other-secret"}}})
        self.registered = []
        self.context = types.SimpleNamespace(register_web_api=lambda *args: self.registered.append(args))
        self.uploaded = None

    async def web_state(self):
        return {"persona": {"name": "test"}}

    async def web_save_settings(self, body):
        if body.get("fail"):
            raise ValueError("test-secret other-secret Bearer bearer-secret https://test.invalid?api_key=query-secret")
        return body

    async def web_job(self, id):
        return {"id": id, "status": "succeeded"}

    async def web_upload_reference(self, data, name, persona_id=""):
        self.uploaded = (data, name, persona_id)
        return {"asset": "reference.png"}


class NativeRequest:
    method = "GET"
    content_type = "application/json"

    def __init__(self, body=None, files=None):
        self.payload = body
        self.file_values = files or {}

    async def json(self, default=None):
        return self.payload if self.payload is not None else default

    async def files(self):
        return self.file_values


class PageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.plugin = Plugin(Path(self.directory.name))
        self.page = page_api.CanvasPageApi(self.plugin)

    async def asyncTearDown(self):
        self.directory.cleanup()

    def adapter(self, request, modern=True):
        module = types.ModuleType("astrbot.api.web" if modern else "quart")
        module.request = request
        if modern:
            module.json_response = lambda payload: {"json": payload}
            module.error_response = lambda message, status_code=400: {"error": message, "code": status_code}
            module.file_response = lambda path, content_type="": {"file": str(path), "mime": content_type}
            return patch.dict(sys.modules, {"astrbot.api.web": module})
        module.jsonify = lambda payload: {"json": payload}
        module.send_file = lambda path, mimetype="": {"file": str(path), "mime": mimetype}
        return patch.dict(sys.modules, {"astrbot.api.web": None, "quart": module})

    async def test_modern_get_post_errors_and_dynamic_routes(self):
        self.assertTrue(self.page.register_routes())
        names = {item[0] for item in self.plugin.registered}
        self.assertIn(page_api.PAGE_PREFIX + "/jobs/<id>", names)
        self.assertIn(page_api.PAGE_PREFIX + "/reference/upload", names)
        self.assertIn(page_api.PAGE_PREFIX + "/simulate", names)
        request = NativeRequest({"integration": {"enabled": True}})
        with self.adapter(request):
            self.assertEqual((await self.page._handler("state")())["json"]["persona"]["name"], "test")
            request.method = "POST"
            self.assertEqual((await self.page._handler("settings")())["json"], request.payload)
            request.payload = {"fail": True}
            error = await self.page._handler("settings")()
            self.assertEqual(error["code"], 400)
            for secret in ("test-secret", "other-secret", "bearer-secret", "query-secret"):
                self.assertNotIn(secret, error["error"])
            self.assertEqual((await self.page.job(id="job-1"))["json"]["id"], "job-1")

    async def test_quart_fallback_json_and_dynamic_asset(self):
        request = types.SimpleNamespace(method="POST", content_type="application/json")
        async def get_json(silent=False):
            return {"enabled": True}
        request.get_json = get_json
        asset = self.plugin.storage.assets / "reference.png"
        Image.new("RGB", (10, 10), "blue").save(asset)
        with self.adapter(request, modern=False):
            self.assertEqual((await self.page._handler("settings")())["json"], {"enabled": True})
            response = await self.page.asset(name="reference.png")
            self.assertEqual(response["mime"], "image/png")
            self.assertEqual(response["file"], str(asset))
            bad, status = await self.page.asset(name="../secret.png")
            self.assertEqual(status, 404)
            self.assertEqual(bad["json"]["status"], "error")

    async def test_modern_dynamic_paths_from_public_request_context(self):
        request = NativeRequest()
        asset = self.plugin.storage.assets / "reference.png"
        Image.new("RGB", (10, 10), "blue").save(asset)
        with self.adapter(request):
            request.path_params = {"id": "job-context"}
            self.assertEqual((await self.page.job())["json"]["id"], "job-context")
            request.path_params = {"name": "reference.png"}
            self.assertEqual((await self.page.reference_preview())["json"]["asset"], "reference.png")
            self.assertEqual((await self.page.asset())["file"], str(asset))

    async def test_native_multipart_upload_and_json_fallback(self):
        class Upload:
            filename = "input.png"
            async def save(self, path):
                Path(path).write_bytes(b"sample-image")
        request = NativeRequest(files={"file": Upload()})
        request.content_type = "multipart/form-data; boundary=test"
        with self.adapter(request):
            self.assertEqual((await self.page.upload())["json"], {"asset": "reference.png"})
            self.assertEqual(self.plugin.uploaded, (b"sample-image", "input.png", ""))
            request.content_type = "application/json"
            request.payload = {"data": base64.b64encode(b"other-image").decode(), "name": "other.png", "persona_id": "other"}
            await self.page.upload()
            self.assertEqual(self.plugin.uploaded, (b"other-image", "other.png", "other"))
            request.payload["data"] = "invalid base64"
            self.assertEqual((await self.page.upload())["code"], 400)

    async def test_thumbnail_and_asset_traversal(self):
        path = self.plugin.storage.assets / "reference.png"
        Image.new("RGB", (2200, 1200), "blue").save(path)
        result = page_api.image_data_url(self.plugin, path.name, thumbnail=True)
        decoded = base64.b64decode(result.split(",", 1)[1])
        with Image.open(io.BytesIO(decoded)) as picture:
            self.assertLessEqual(max(picture.size), 384)
        self.assertLess(len(decoded), 80_000)
        self.assertEqual(page_api.image_data_url(self.plugin, "../reference.png"), "")
        self.assertEqual((await page_api.dispatch_api(self.plugin, "GET", "page/reference/preview/reference.png"))["asset"], "reference.png")


class LegacyHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.plugin = Plugin(Path(self.directory.name))
        (self.plugin.storage.assets / "reference.png").write_bytes(b"mock-image")
        self.server = webui.WebUI(self.plugin, host="127.0.0.1", port=0, token="test-token")
        self.server.start()

    async def asyncTearDown(self):
        await self.server.close()
        self.directory.cleanup()

    async def request(self, path, body=None, authorized=True, origin=None):
        def perform():
            headers = {"Authorization": "Bearer test-token"} if authorized else {}
            if origin:
                headers["Origin"] = origin
            data = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body).encode()
            request = Request(self.server.url + path, data=data, headers=headers)
            try:
                response = urlopen(request, timeout=5)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, response.read(), response.headers
        return await asyncio.to_thread(perform)

    async def test_public_static_and_authenticated_api_and_assets(self):
        status, body, headers = await self.request("/", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn(b"persona-form", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual((await self.request("/api/page/state", authorized=False))[0], 401)
        self.assertEqual((await self.request("/assets/reference.png", authorized=False))[0], 401)
        status, body, _ = await self.request("/api/page/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["persona"]["name"], "test")
        self.assertEqual((await self.request("/assets/reference.png"))[1], b"mock-image")
        self.assertEqual((await self.request("/api/state"))[0], 200)

    async def test_same_origin_write_upload_errors_and_job_route(self):
        self.assertEqual((await self.request("/api/page/settings", {"enabled": True}, origin="https://outside.invalid"))[0], 403)
        status, body, _ = await self.request("/api/page/settings", {"enabled": True}, origin=self.server.url)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"enabled": True})
        status, body, _ = await self.request("/api/page/reference/upload", {"data": base64.b64encode(b"sample-image").decode(), "name": "input.png"})
        self.assertEqual(status, 200)
        self.assertEqual(self.plugin.uploaded, (b"sample-image", "input.png", ""))
        self.assertEqual((await self.request("/api/page/reference/upload", {"data": "bad!!!"}))[0], 400)
        self.assertEqual(json.loads((await self.request("/api/page/jobs/job-1"))[1])["id"], "job-1")
        status, body, _ = await self.request("/api/page/settings", {"fail": True})
        self.assertEqual(status, 400)
        self.assertNotIn(b"test-secret", body)
        self.assertNotIn(b"bearer-secret", body)


if __name__ == "__main__":
    unittest.main()
