from __future__ import annotations

import base64
import copy
import mimetypes
import re
from pathlib import Path

from astrbot.api.web import error_response, file_response, request


PLUGIN_NAME = "astrbot_plugin_persona_canvas"
PAGE_PREFIX = f"/{PLUGIN_NAME}/page"
_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}$")


class CanvasPageApi:
    def __init__(self, plugin):
        self.plugin = plugin

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("state", self.state, ["GET"]),
            ("personas", self.personas, ["GET", "POST"]),
            ("providers", self.providers, ["GET", "POST"]),
            ("settings", self.settings, ["POST"]),
            ("generate", self.generate, ["POST"]),
            ("targets", self.targets, ["GET", "POST"]),
            ("history", self.history, ["GET"]),
            ("assets/<name>", self.asset, ["GET"]),
            ("llm/providers", self.llm_providers, ["GET"]),
            ("llm/test", self.llm_test, ["POST"]),
            ("llm/save", self.llm_save, ["POST"]),
            ("image/models", self.image_models, ["POST"]),
            ("image/test", self.image_test, ["POST"]),
        ]
        for suffix, handler, methods in routes:
            register(f"{PAGE_PREFIX}/{suffix}", handler, methods, f"Persona Canvas page: {suffix}")

    async def state(self):
        return await self.plugin.web_state()

    async def personas(self):
        if request.method == "GET":
            return await self.plugin.web_personas()
        body = await request.json(default={})
        return await self.plugin.web_save_persona(body if isinstance(body, dict) else {})

    async def providers(self):
        if request.method == "GET":
            return await self.plugin.web_providers()
        body = await request.json(default={})
        return await self.plugin.web_save_provider(body if isinstance(body, dict) else {})

    async def settings(self):
        body = await request.json(default={})
        return await self.plugin.web_save_settings(body if isinstance(body, dict) else {})

    async def generate(self):
        body = await request.json(default={})
        try:
            return await self.plugin.web_generate(body if isinstance(body, dict) else {})
        except Exception as exc:
            return error_response(str(exc), status_code=400)

    async def targets(self):
        if request.method == "GET":
            return await self.plugin.web_targets()
        body = await request.json(default={})
        return await self.plugin.web_target_action(body if isinstance(body, dict) else {})

    async def history(self):
        return await self.plugin.web_history()

    async def llm_providers(self):
        try:
            return await self.plugin._llm_providers()
        except Exception as exc:
            return error_response(str(exc), status_code=400)

    async def llm_test(self):
        body = await request.json(default={})
        try:
            return await self.plugin._test_llm(body if isinstance(body, dict) else {})
        except Exception as exc:
            return error_response(str(exc), status_code=400)

    async def llm_save(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("LLM 设置格式无效", status_code=400)
        settings = self.plugin.storage.settings.setdefault("llm", {})
        for key in ("provider_id", "model", "fallback_to_current", "timeout_sec"):
            if key in body:
                settings[key] = body[key]
        settings["timeout_sec"] = max(5, min(300, int(settings.get("timeout_sec", 45))))
        self.plugin.storage.save_settings()
        return {"settings": settings}

    async def image_models(self):
        body = await request.json(default={})
        name = str(body.get("name") or "") if isinstance(body, dict) else ""
        try:
            provider = self.plugin._provider(name)
            result = await provider.list_models()
            return {"provider": name, **result}
        except Exception as exc:
            return error_response(str(exc), status_code=400)

    async def image_test(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("测试参数无效", status_code=400)
        name = str(body.get("name") or "")
        provider = self.plugin._provider(name)
        if not body.get("generate_image"):
            return await provider.test_connection()
        try:
            result, elapsed = await provider.test_generation(str(body.get("prompt") or "simple blue flower on white background"))
            data_url = f"data:image/{'jpeg' if result.extension == 'jpg' else result.extension};base64,{base64.b64encode(result.data).decode('ascii')}"
            return {"ok": True, "provider": name, "model": result.model, "extension": result.extension, "elapsed_ms": elapsed, "image": data_url}
        except Exception as exc:
            return {"ok": False, "provider": name, "model": provider.config.get("model", ""), "message": str(exc)[:500]}

    async def asset(self):
        name = str(request.path_params.get("name") or "")
        if not _ASSET_NAME.fullmatch(name) or Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            return error_response("资源路径无效", status_code=404)
        path = self.plugin.storage.assets / name
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            return error_response("资源不存在", status_code=404)
        return file_response(path, content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


def image_data_url(plugin, name: str) -> str:
    if not _ASSET_NAME.fullmatch(name):
        return ""
    path = plugin.storage.assets / name
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
