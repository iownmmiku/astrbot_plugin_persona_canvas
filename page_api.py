from __future__ import annotations

import asyncio
import base64
import inspect
import io
import mimetypes
import re
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

try:
    from .common import redact_text
except ImportError:  # Loaded as a top-level module by standalone tools/tests.
    from common import redact_text

PLUGIN_NAME = "astrbot_plugin_persona_canvas"
PAGE_PREFIX = f"/{PLUGIN_NAME}/page"
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
THUMBNAIL_CACHE_ENTRIES = 96
THUMBNAIL_CACHE_BYTES = 8 * 1024 * 1024
_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}$")

GET_ROUTES = {
    "diagnostics": "web_diagnostics",
    "state": "web_state",
    "personas": "web_personas",
    "providers": "web_providers",
    "targets": "web_targets",
    "sessions": "web_sessions",
    "llm/providers": "_llm_providers",
    "export": "web_export",
}
POST_ROUTES = {
    "jobs/cancel": "web_cancel_job",
    "jobs/resend": "web_resend_job",
    "jobs/query": "web_jobs_query",
    "history/query": "web_history_query",
    "jobs/recover": "web_recover_job",
    "targets/inspect": "web_inspect_target",
    "settings": "web_save_settings",
    "personas": "web_save_persona",
    "personas/delete": "web_delete_persona",
    "providers": "web_save_provider",
    "providers/delete": "web_delete_provider",
    "targets": "web_target_action",
    "generate": "web_generate",
    "jobs/retry": "web_retry_job",
    "llm/test": "_test_llm",
    "image/models": "_image_models",
    "image/test": "_image_test",
    "simulate": "web_simulate",
}


def public_error(plugin, exc: Exception) -> str:
    """Console error text: credentials hidden, upstream detail kept for diagnosis."""
    message = str(exc) or "操作失败，请查看 AstrBot 日志"
    detail = str(getattr(exc, "detail", "") or "")
    return redact_text(plugin.storage, message + (f"（上游详情：{detail}）" if detail else ""))


def asset_path(plugin, name: str) -> Path:
    if not _ASSET_NAME.fullmatch(name) or Path(name).suffix.lower() not in {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".avif",
    }:
        raise ValueError("资源路径无效")
    root = plugin.storage.assets.resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("资源不存在")
    return path


def preview_descriptor(plugin, name: str) -> dict[str, str] | None:
    """Small immutable-image identity for metadata polling and lazy previews."""
    try:
        path = asset_path(plugin, name)
        stat = path.stat()
        return {"asset": name, "version": f"{stat.st_mtime_ns:x}-{stat.st_ctime_ns:x}-{stat.st_size:x}"}
    except (ValueError, OSError):
        cache = getattr(plugin, "_canvas_thumbnail_cache", None)
        if cache is not None:
            cache.pop(name, None)
        return None


def _render_thumbnail(path: Path) -> str:
    """CPU-bound: decode, orient and re-encode a small JPEG preview."""
    from PIL import Image, ImageOps

    with Image.open(path) as source:
        picture = ImageOps.exif_transpose(source).convert("RGB")
        picture.thumbnail((384, 384))
        buffer = io.BytesIO()
        picture.save(buffer, "JPEG", quality=75, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _full_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _thumbnail_cache(plugin) -> OrderedDict:
    cache = getattr(plugin, "_canvas_thumbnail_cache", None)
    if cache is None:
        cache = plugin._canvas_thumbnail_cache = OrderedDict()
    return cache


def _cached_thumbnail(plugin, name: str, version: str) -> str:
    cache = _thumbnail_cache(plugin)
    cached = cache.get(name)
    if cached and cached[0] == version:
        cache.move_to_end(name)
        return cached[1]
    return ""


def _store_thumbnail(plugin, name: str, version: str, value: str) -> None:
    cache = _thumbnail_cache(plugin)
    cache[name] = (version, value)
    cache.move_to_end(name)
    plugin._canvas_thumbnail_bytes = sum(len(item[1]) for item in cache.values())
    while cache and (len(cache) > THUMBNAIL_CACHE_ENTRIES or plugin._canvas_thumbnail_bytes > THUMBNAIL_CACHE_BYTES):
        _old, (_version, dropped) = cache.popitem(last=False)
        plugin._canvas_thumbnail_bytes -= len(dropped)


def _drop_thumbnail(plugin, name: str) -> None:
    cache = getattr(plugin, "_canvas_thumbnail_cache", None)
    if cache is not None:
        cache.pop(name, None)


def image_data_url(plugin, name: str, thumbnail: bool = False) -> str:
    """Synchronous variant for tools/tests; request handlers use the async one."""
    try:
        path = asset_path(plugin, name)
        if not thumbnail:
            return _full_data_url(path)
        descriptor = preview_descriptor(plugin, name)
        if descriptor is None:
            return ""
        result = _cached_thumbnail(plugin, name, descriptor["version"])
        if not result:
            result = _render_thumbnail(path)
            _store_thumbnail(plugin, name, descriptor["version"], result)
        return result
    except (ValueError, OSError, ImportError):
        _drop_thumbnail(plugin, name)
        return ""


async def image_data_url_async(plugin, name: str, thumbnail: bool = False) -> str:
    """Same result as image_data_url, with file IO and image work in a thread.

    The cache is only touched on the event loop, never from the worker.
    """
    try:
        path = asset_path(plugin, name)
        if not thumbnail:
            return await asyncio.to_thread(_full_data_url, path)
        descriptor = preview_descriptor(plugin, name)
        if descriptor is None:
            return ""
        result = _cached_thumbnail(plugin, name, descriptor["version"])
        if not result:
            result = await asyncio.to_thread(_render_thumbnail, path)
            _store_thumbnail(plugin, name, descriptor["version"], result)
        return result
    except (ValueError, OSError, ImportError):
        _drop_thumbnail(plugin, name)
        return ""


async def dispatch_api(plugin, method: str, path: str, body: dict | None = None) -> Any:
    path = path.removeprefix("page/").strip("/")
    if method == "GET" and path.startswith("jobs/"):
        return await plugin.web_job(path.removeprefix("jobs/"))
    if method == "GET" and path.startswith("reference/preview/"):
        name = path.removeprefix("reference/preview/")
        return {
            "asset": name,
            "image": await image_data_url_async(plugin, name, thumbnail=True),
            "thumbnail": preview_descriptor(plugin, name),
        }
    routes = GET_ROUTES if method == "GET" else POST_ROUTES
    callback = routes.get(path)
    if callback is None:
        raise LookupError("接口不存在")
    if method == "GET":
        return await getattr(plugin, callback)()
    return await getattr(plugin, callback)(body or {})


def _web_adapter():
    """Legacy AstrBot can load the plugin without the new api.web module."""
    try:
        from astrbot.api.web import error_response, file_response, json_response, request

        return request, json_response, error_response, file_response, True
    except ImportError:
        from quart import jsonify, request, send_file

        return (
            request,
            jsonify,
            lambda message, status_code=400: (jsonify({"status": "error", "message": message}), status_code),
            send_file,
            False,
        )


class CanvasPageApi:
    def __init__(self, plugin):
        self.plugin = plugin

    def register_routes(self) -> bool:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            return False
        for suffix in sorted(set(GET_ROUTES) | set(POST_ROUTES)):
            methods = [method for method, routes in (("GET", GET_ROUTES), ("POST", POST_ROUTES)) if suffix in routes]
            register(f"{PAGE_PREFIX}/{suffix}", self._handler(suffix), methods, f"随想画卷: {suffix}")
        register(f"{PAGE_PREFIX}/jobs/<id>", self.job, ["GET"], "随想画卷: 任务详情")
        register(f"{PAGE_PREFIX}/assets/<name>", self.asset, ["GET"], "随想画卷: 图片资源")
        register(f"{PAGE_PREFIX}/reference/upload", self.upload, ["POST"], "随想画卷: 上传参考图")
        register(f"{PAGE_PREFIX}/reference/preview/<name>", self.reference_preview, ["GET"], "随想画卷: 参考图预览")
        return True

    def _handler(self, suffix: str):
        async def handler(**_kwargs):
            request, jsonify, error, _file, modern = _web_adapter()
            try:
                body = None
                if request.method == "POST":
                    body = await request.json(default={}) if modern else await request.get_json(silent=True)
                    if not isinstance(body, dict):
                        raise ValueError("请求必须是 JSON 对象")
                return jsonify(await dispatch_api(self.plugin, request.method, suffix, body))
            except LookupError as exc:
                return error(public_error(self.plugin, exc), status_code=404)
            except Exception as exc:
                return error(public_error(self.plugin, exc), status_code=400)

        handler.__name__ = "canvas_" + suffix.replace("/", "_")
        return handler

    async def job(self, id: str = "", **_kwargs):
        request, jsonify, error, _file, modern = _web_adapter()
        try:
            if not id and modern:
                id = str(request.path_params.get("id") or "")
            return jsonify(await self.plugin.web_job(id))
        except Exception as exc:
            return error(public_error(self.plugin, exc), status_code=404)

    async def reference_preview(self, name: str = "", **_kwargs):
        request, jsonify, error, _file, modern = _web_adapter()
        try:
            if not name and modern:
                name = str(request.path_params.get("name") or "")
            asset_path(self.plugin, name)
            return jsonify(
                {
                    "asset": name,
                    "image": await image_data_url_async(self.plugin, name, thumbnail=True),
                    "thumbnail": preview_descriptor(self.plugin, name),
                }
            )
        except Exception as exc:
            return error(public_error(self.plugin, exc), status_code=404)

    async def asset(self, name: str = "", **_kwargs):
        request, _json, error, file_response, modern = _web_adapter()
        try:
            if not name and modern:
                name = str(request.path_params.get("name") or "")
            path = asset_path(self.plugin, name)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            result = file_response(path, content_type=mime) if modern else file_response(path, mimetype=mime)
            return await result if inspect.isawaitable(result) else result
        except (ValueError, OSError) as exc:
            return error(public_error(self.plugin, exc), status_code=404)

    async def upload(self, **_kwargs):
        request, jsonify, error, _file, modern = _web_adapter()
        try:
            if "multipart/form-data" in str(request.content_type or ""):
                files = await request.files() if modern else await request.files
                file = files.get("file")
                if file is None:
                    raise ValueError("请选择参考图")
                filename = str(getattr(file, "filename", "reference.png"))
                reader = getattr(file, "read", None) or getattr(getattr(file, "stream", None), "read", None)
                if reader:
                    try:
                        data = reader(MAX_UPLOAD_BYTES + 1)
                    except TypeError:
                        data = reader()
                    if inspect.isawaitable(data):
                        data = await data
                else:
                    with tempfile.TemporaryDirectory(prefix="persona-reference-") as directory:
                        path = Path(directory) / "upload"
                        result = file.save(path)
                        if inspect.isawaitable(result):
                            await result
                        if path.stat().st_size > MAX_UPLOAD_BYTES:
                            raise ValueError("参考图不能超过 16 MB")
                        data = path.read_bytes()
                persona_id = ""
            else:
                body = await request.json(default={}) if modern else await request.get_json(silent=True)
                if not isinstance(body, dict):
                    raise ValueError("请求格式无效")
                encoded = str(body.get("data") or "")
                if len(encoded) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
                    raise ValueError("参考图不能超过 16 MB")
                data = base64.b64decode(encoded, validate=True)
                filename = str(body.get("name") or "reference.png")
                persona_id = str(body.get("persona_id") or "")
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError("参考图不能超过 16 MB")
            return jsonify(await self.plugin.web_upload_reference(data, filename, persona_id))
        except Exception as exc:
            return error(public_error(self.plugin, exc), status_code=400)
