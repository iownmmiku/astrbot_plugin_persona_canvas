"""Explicit, mockable image API adapters.

options['_explicit'] names parameters deliberately supplied for this request.
Shared defaults may be adapted to a model; unsupported explicit parameters fail.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import io
import ipaddress
import json
import math
import re
import secrets
import socket
import time
import warnings
import zipfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 36_000_000


class ProviderError(RuntimeError):
    pass


@dataclass
class GeneratedImage:
    data: bytes
    extension: str = "png"
    provider: str = ""
    model: str = ""


@dataclass
class ProviderCapabilities:
    text_to_image: bool = True
    image_to_image: bool = False
    negative_prompt: bool = False
    seed: bool = False
    dimensions: str = "由模型决定"
    sampler: bool = False
    identity_reference: str = "不保证角色身份一致性"
    negative_mode: str = "disabled"
    negative_prompt_mode: str = "未发送负面提示词"
    automated_generation: bool = True
    max_reference_images: int = 1


def _path_get(value: Any, path: str) -> Any:
    for part in path.split(".") if path else []:
        if isinstance(value, list) and part.isdigit():
            index = int(part)
            value = value[index] if index < len(value) else None
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _path_set(value: dict, path: str, item: Any) -> None:
    parts = path.split(".")
    if not path or any(not part for part in parts):
        raise ProviderError("自定义字段路径不能为空")
    for part in parts[:-1]:
        node = value.setdefault(part, {})
        if not isinstance(node, dict):
            raise ProviderError("自定义字段路径相互冲突")
        value = node
    value[parts[-1]] = item


def _validate_field_paths(fields: list[tuple[str, Any]]) -> None:
    """Reject ambiguous request mappings before one value replaces another."""
    seen: list[tuple[str, str]] = []
    for label, path in fields:
        if not isinstance(path, str) or not path.strip() or any(not part for part in path.split(".")):
            raise ProviderError(f"自定义字段路径无效：{label}")
        for previous_label, previous_path in seen:
            if path == previous_path or path.startswith(previous_path + ".") or previous_path.startswith(path + "."):
                raise ProviderError(f"请求字段映射冲突：{previous_label} 与 {label}（{previous_path} / {path}）")
        seen.append((label, path))


def _image_info(data: bytes) -> tuple[str, str, tuple[int, int]]:
    """Verify actual pixels instead of trusting a MIME header or file suffix."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ProviderError("图片为空或超过 32 MB")
    try:
        from PIL import Image
    except ImportError as exc:
        raise ProviderError("缺少 Pillow，无法验证图片内容") from exc
    formats = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp"), "GIF": ("gif", "image/gif")}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                size, fmt = picture.size, picture.format
                if fmt not in formats or min(size) < 1 or size[0] * size[1] > MAX_IMAGE_PIXELS:
                    raise ProviderError("图片格式或像素尺寸不受支持")
                picture.verify()
            with Image.open(io.BytesIO(data)) as picture:
                picture.load()
        extension, mime = formats[fmt]
        return extension, mime, size
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError("接口返回的图片损坏或不是可识别图片") from exc


def _image_bytes(value: str) -> tuple[bytes, str]:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("接口返回的图片字段为空")
    declared = ""
    if value.startswith("data:"):
        header, separator, value = value.partition(",")
        if not separator or ";base64" not in header.lower() or not header.lower().startswith("data:image/"):
            raise ProviderError("不支持的图片 Data URL")
        declared = header[5:].split(";", 1)[0].lower().replace("image/jpg", "image/jpeg")
    encoded = re.sub(r"\s+", "", value)
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ProviderError("图片字段超过 32 MB")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProviderError("图片字段不是有效 Base64") from exc
    extension, mime, _ = _image_info(data)
    if declared and declared != mime:
        raise ProviderError("图片 Data URL 的 MIME 与实际内容不匹配")
    return data, extension


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
        if isinstance(value, bool) or float(value) != number or not minimum <= number <= maximum:
            raise ValueError
        return number
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProviderError(f"{label} 必须为 {minimum}–{maximum} 之间的整数") from exc


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
        if isinstance(value, bool) or not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError
        return number
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProviderError(f"{label} 超出支持范围") from exc


class ImageProvider:
    """Shared transport. Prefer provider_from_config() for construction."""
    supported_options: set[str] = set()
    def __new__(cls, name: str, config: dict[str, Any]):
        if cls is ImageProvider:
            if not isinstance(config, dict):
                raise ProviderError("绘画接口配置必须为对象")
            adapters = {"openai": OpenAIProvider, "gemini": GeminiProvider, "novelai": NovelAIProvider, "custom": CustomProvider}
            kind = str(config.get("kind") or "openai").strip().lower()
            if kind not in adapters:
                raise ProviderError(f"不支持的绘画接口类型：{kind}")
            return object.__new__(adapters[kind])
        return object.__new__(cls)

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = str(name)
        self.config = copy.deepcopy(config)
        self.last_request: dict[str, Any] = {}
        self.capabilities = ProviderCapabilities(image_to_image=bool(config.get("supports_image_edit", False)))
        kind = getattr(self, "provider_kind", str(config.get("kind") or "openai").lower().strip())
        mode = str(config.get("negative_mode") or ("field" if kind == "custom" and config.get("negative_prompt") else "disabled")).strip().lower()
        if mode not in {"disabled", "natural_language", "field"}:
            raise ProviderError("negative_mode 必须为 disabled、natural_language 或 field")
        if kind == "novelai":
            mode = "field"
        if kind == "gemini" and mode == "field":
            raise ProviderError("Gemini 原生接口不支持独立负面字段，请使用 disabled 或显式 natural_language")
        self.capabilities.negative_mode = mode
        self.capabilities.negative_prompt = mode == "field"
        self.capabilities.negative_prompt_mode = {
            "disabled": "未发送负面提示词",
            "natural_language": "显式自然语言避免约束，写入正面描述（非独立负面通道）",
            "field": "独立负面字段：" + str(config.get("negative_prompt_field") or "negative_prompt"),
        }[mode]

    def validate_config(self) -> None:
        """Validate configuration without issuing a network or image request."""
        _integer(self.config.get("timeout", 180), "超时", 5, 600)
        extra = self.config.get("extra_body") or {}
        if not isinstance(extra, dict):
            raise ProviderError("extra_body 必须为 JSON 对象")
        try:
            json.dumps(extra, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ProviderError("extra_body 必须为有效 JSON 对象") from exc
        if self.config.get("endpoint"):
            for route in ("generation_path", "edit_path", "models_path", "connection_path"):
                self._url(route, "")

    def _record_request(self, prompt: Any, negative: Any, model: str, options: dict, reference: Any, *, negative_field: str = "", notes: list[str] | None = None) -> None:
        """Keep only inspectable request values, never headers or image payloads."""
        allowed = {"width", "height", "size", "steps", "scale", "sampler", "seed", "strength", "noise", "quality", "background", "output_format", "input_fidelity", "aspect_ratio", "image_size", "params_version", "extra_noise_seed"}
        secret = str(self.config.get("api_key") or "")
        def safe(value):
            if isinstance(value, str):
                return value.replace(secret, "[REDACTED]") if secret else value
            if value is None or isinstance(value, (bool, int)) or isinstance(value, float) and math.isfinite(value):
                return value
            return None
        mode = self.capabilities.negative_mode
        self.last_request = {
            "prompt": safe(str(prompt)),
            "negative_prompt": safe(str(negative or "")) if negative_field else "",
            "negative_mode": mode,
            "negative_prompt_field": safe(negative_field),
            "negative_source": "plugin" if mode == "field" else "extra_body" if negative_field else "none",
            "model": safe(model),
            "options": {key: safe(value) for key, value in options.items() if key in allowed and isinstance(value, (str, int, float, bool, type(None)))},
            "reference_count": len(reference) if isinstance(reference, list) else int(reference is not None),
            "provider_timeout_sec": _integer(self.config.get("timeout", 180), "超时", 5, 600),
        }
        details = list(notes or [])
        if mode == "natural_language":
            details.append("负面约束已编入正面描述，插件未自动传入独立负面字段")
        if negative_field and mode != "field":
            details.append("独立负面字段来自手动 extra_body 配置；插件未自动传入人设负面词")
        if details:
            self.last_request["notes"] = [safe(str(item)) for item in details]

    def _safe_error(self, message: Any) -> str:
        value = str(message)
        key = str(self.config.get("api_key") or "")
        if key:
            value = value.replace(key, "[REDACTED]").replace(quote(key, safe=""), "[REDACTED]")
        value = re.sub(r"(?i)(bearer\s+)[^\s\"'<>]+", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)(api[_-]?key|token|authorization)([\"'\s:=]+)[^\s,}\"']+", r"\1\2[REDACTED]", value)
        return value[:500]

    def _headers(self) -> dict[str, str]:
        result = {"Accept": "application/json", "Content-Type": "application/json"}
        key = str(self.config.get("api_key") or "")
        if key:
            result[str(self.config.get("auth_header") or "Authorization")] = f"{self.config.get('auth_prefix', 'Bearer ')}{key}"
        return result

    @staticmethod
    async def _read_response(response, limit: int) -> bytes:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ProviderError("接口响应超过大小限制")
        chunks, size = [], 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > limit:
                raise ProviderError("接口响应超过大小限制")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _http(self, method: str, url: str, body: dict | None = None, *, files: dict[str, bytes] | None = None, limit: int = MAX_RESPONSE_BYTES) -> tuple[bytes, str, int]:
        try:
            import aiohttp
        except ImportError as exc:
            raise ProviderError("AstrBot 环境缺少 aiohttp") from exc
        headers = self._headers()
        timeout = aiohttp.ClientTimeout(total=_integer(self.config.get("timeout", 180), "超时", 5, 600))
        request_body = None
        if files:
            headers.pop("Content-Type", None)
            request_body = aiohttp.FormData()
            for key, value in (body or {}).items():
                request_body.add_field(key, json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (dict, list, bool)) else str(value))
            for key, values in files.items():
                for index, data in enumerate(values if isinstance(values, list) else [values]):
                    extension, mime, _ = _image_info(data)
                    request_body.add_field(key, data, filename=f"reference-{index}.{extension}", content_type=mime)
        elif body is not None:
            request_body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=headers, data=request_body, allow_redirects=False) as response:
                    raw = await self._read_response(response, limit)
                    if not 200 <= response.status < 300:
                        hint = "检查 Key、权限和模型" if response.status in {401, 403} else "接口限流，请稍后再试" if response.status == 429 else "检查接口地址及参数"
                        # Error bodies can echo private request JSON or credentials.
                        raise ProviderError(f"HTTP {response.status}：{hint}")
                    return raw, response.headers.get("Content-Type", ""), response.status
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(self._safe_error(f"接口请求失败：{exc}")) from None

    async def _post(self, url: str, body: dict, *, files: dict[str, bytes] | None = None) -> tuple[bytes, str]:
        raw, content_type, _ = await self._http("POST", url, body, files=files)
        return raw, content_type

    async def _request(self, method: str, url: str, body: dict | None = None) -> tuple[bytes, str, int]:
        return await self._http(method, url, body, limit=8 * 1024 * 1024)

    @staticmethod
    def _endpoint_path(endpoint: str, suffix: str) -> str:
        base = endpoint.rstrip("/")
        if not suffix or base.endswith(suffix):
            return base
        if base.endswith("/v1") and suffix.startswith("/v1/"):
            return base + suffix[3:]
        return base + "/" + suffix.lstrip("/")

    def _url(self, key: str, default: str) -> str:
        endpoint = str(self.config.get("endpoint") or "").strip().rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProviderError("接口地址必须为不含账号、查询参数的 HTTP(S) 基础地址")
        route = str(self.config.get(key) or "").strip()
        if not route and key == "generation_path":
            route = str(self.config.get("generate_path") or "").strip()
        route = route or default
        if route.startswith(("http://", "https://")):
            if self._origin(route) != self._origin(endpoint):
                raise ProviderError("接口路由必须与接口地址同源")
            return route
        return self._endpoint_path(endpoint, route)

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int]:
        value = urlsplit(url)
        try:
            return value.scheme.lower(), (value.hostname or "").lower(), value.port or (443 if value.scheme == "https" else 80)
        except ValueError as exc:
            raise ProviderError("URL 端口无效") from exc

    def _merge_extra(self, body: dict, protected: set[str]) -> dict:
        extra = self.config.get("extra_body") or {}
        if not isinstance(extra, dict):
            raise ProviderError("extra_body 必须为 JSON 对象")
        def merge(target: dict, patch: dict, prefix: str = ""):
            for key, value in patch.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if any(path == field or path.startswith(field + ".") for field in protected):
                    raise ProviderError(f"extra_body 不能覆盖请求字段：{path}")
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value, path)
                else:
                    if any(field.startswith(path + ".") for field in protected):
                        raise ProviderError(f"extra_body 不能替换受保护字段：{path}")
                    target[key] = copy.deepcopy(value)
        result = copy.deepcopy(body)
        merge(result, extra)
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ProviderError("请求参数不是有效 JSON") from exc
        return result

    def _prepare(self, prompt: str, negative: str, reference: bytes | None, options: dict | None) -> tuple[str, dict]:
        self.last_request = {}
        self.validate_config()
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError("生图描述不能为空")
        if reference is not None:
            if not self.capabilities.image_to_image:
                raise ProviderError("当前接口未启用图生图，请选择支持图像编辑的接口")
            refs = reference if isinstance(reference, list) else [reference]
            if not refs or len(refs) > self.capabilities.max_reference_images:
                raise ProviderError(f"此接口单次最多支持 {self.capabilities.max_reference_images} 张参考图")
            for data in refs:
                _image_info(data)
            if sum(len(data) for data in refs) > 48 * 1024 * 1024:
                raise ProviderError("参考图总大小超过 48 MB")
        if options is not None and not isinstance(options, dict):
            raise ProviderError("生图参数必须为对象")
        options = copy.deepcopy(options or {})
        explicit = options.pop("_explicit", [])
        if not isinstance(explicit, (list, tuple, set)):
            raise ProviderError("显式参数列表格式错误")
        options["_explicit"] = set(str(key) for key in explicit)
        for key in options["_explicit"]:
            if key in options and key not in self.supported_options:
                raise ProviderError(f"当前接口不支持参数：{key}")
        if not self.capabilities.seed and options.get("seed", -1) not in (None, -1, "-1"):
            raise ProviderError("当前接口不支持固定随机种子")
        for key in ("steps", "scale", "sampler", "seed"):
            supported = self.capabilities.seed if key == "seed" else self.capabilities.sampler
            if key in options["_explicit"] and key in options and not supported:
                raise ProviderError(f"当前接口不支持参数：{key}")
        if negative and self.capabilities.negative_mode == "natural_language":
            prompt = f"{prompt.strip()}\n\nAvoid these visual elements and defects: {str(negative).strip()}"
        return prompt.strip(), options

    async def generate(self, prompt: str, negative_prompt: str, *, reference: bytes | None = None, options: dict[str, Any] | None = None) -> GeneratedImage:
        raise ProviderError("此接口没有实现生图，请使用具体绘画适配器")

    def _json(self, raw: bytes) -> Any:
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("接口没有返回有效 JSON 或图片") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise ProviderError("接口返回错误响应，请检查服务端配置和额度")
        return payload

    def _result(self, data: bytes, model: str) -> GeneratedImage:
        extension, _, _ = _image_info(data)
        return GeneratedImage(data, extension, self.name, model)

    def _decode(self, raw: bytes, content_type: str, model: str) -> GeneratedImage:
        """Synchronous base64/direct/ZIP decoder retained for integrations."""
        if raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8", b"GIF87a", b"GIF89a")) or raw[:4] == b"RIFF" or content_type.lower().startswith("image/"):
            return self._result(raw, model)
        if raw.startswith(b"PK\x03\x04"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    entries = archive.infolist()
                    if len(entries) > 32:
                        raise ProviderError("图片 ZIP 条目过多")
                    for entry in entries:
                        if entry.is_dir() or not entry.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                            continue
                        if entry.file_size > MAX_IMAGE_BYTES:
                            raise ProviderError("ZIP 中的图片超过 32 MB")
                        with archive.open(entry) as handle:
                            image = handle.read(MAX_IMAGE_BYTES + 1)
                        return self._result(image, model)
                raise ProviderError("ZIP 响应中没有图片")
            except ProviderError:
                raise
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ProviderError("接口返回了无效的图片 ZIP") from exc
        value = self._response_value(self._json(raw))
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            raise ProviderError("接口返回图片 URL；需要显式开启 allow_image_urls 并配置允许的图片域名")
        data, extension = _image_bytes(value)
        return GeneratedImage(data, extension, self.name, model)

    def _response_value(self, payload: Any) -> Any:
        path = str(self.config.get("response_path") or "")
        if path:
            value = _path_get(payload, path)
            if value is None:
                raise ProviderError("response_path 指向的图片字段不存在")
            return value
        for path in ("data.0.b64_json", "images.0.image", "data.0.url", "image", "b64_json"):
            value = _path_get(payload, path)
            if value is not None:
                return value
        if isinstance(payload, list) and payload:
            return payload[0]
        raise ProviderError("接口响应中没有图片数据")

    async def _decode_response(self, raw: bytes, content_type: str, model: str) -> GeneratedImage:
        if raw.lstrip().startswith((b"{", b"[")) and not content_type.lower().startswith("image/"):
            value = self._response_value(self._json(raw))
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return self._result(await self._download_image(value), model)
        return self._decode(raw, content_type, model)

    async def _resolve_host(self, host: str, port: int) -> list[str]:
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            entries = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            return list(dict.fromkeys(item[4][0] for item in entries))

    async def _download_image(self, url: str) -> bytes:
        if not self.config.get("allow_image_urls", False):
            raise ProviderError("接口返回图片 URL；需要显式开启 allow_image_urls")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ProviderError("接口返回了不允许的图片 URL")
        endpoint = str(self.config.get("endpoint") or "")
        same_origin = self._origin(url) == self._origin(endpoint)
        allowed = self.config.get("image_url_allowed_hosts", self.config.get("allowed_image_hosts", self.config.get("allowed_hosts", []))) or []
        if not isinstance(allowed, list):
            raise ProviderError("图片 URL 域名白名单必须为列表")
        if not same_origin and (parsed.scheme != "https" or parsed.hostname.lower() not in {str(host).lower() for host in allowed}):
            raise ProviderError("图片 URL 域名不在接口同源或白名单内")
        try:
            addresses = await self._resolve_host(parsed.hostname, self._origin(url)[2])
            if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses) and not same_origin:
                raise ProviderError("不允许下载私有、回环或链路本地地址的图片")
            return await self._download(url, addresses)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(self._safe_error(f"图片下载失败：{exc}")) from None

    async def _download(self, url: str, addresses: list[str]) -> bytes:
        import aiohttp
        class PinnedResolver(aiohttp.abc.AbstractResolver):
            async def resolve(self, host, port=0, family=socket.AF_INET):
                return [{"hostname": host, "host": address, "port": port, "family": socket.AF_INET6 if ":" in address else socket.AF_INET, "proto": 0, "flags": 0} for address in addresses]
            async def close(self):
                pass
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(resolver=PinnedResolver())) as session:
            # Do not forward the provider key, even to same-origin image URLs.
            async with session.get(url, headers={"Accept": "image/*"}, allow_redirects=False) as response:
                if response.status != 200:
                    raise ProviderError(f"图片下载 HTTP {response.status}")
                return await self._read_response(response, MAX_IMAGE_BYTES)

    async def list_models(self) -> dict[str, Any]:
        raw, _, _ = await self._request("GET", self._url("models_path", "/v1/models"))
        payload = self._json(raw)
        values = _path_get(payload, str(self.config.get("models_response_path") or "data"))
        if not isinstance(values, list):
            raise ProviderError("模型列表响应格式错误")
        models = [str(item.get("id") or item.get("name") or "") if isinstance(item, dict) else str(item) for item in values]
        return {"models": sorted(set(value for value in models if value)), "automatic": True, "note": ""}

    async def test_connection(self) -> dict[str, Any]:
        started = time.monotonic()
        result = {"ok": False, "provider": self.name, "model": str(self.config.get("model") or "")}
        try:
            if self.config.get("connection_path"):
                method = str(self.config.get("connection_method") or "GET").upper()
                if method not in {"GET", "HEAD", "OPTIONS"}:
                    raise ProviderError("连接测试只支持无生成副作用的 GET、HEAD、OPTIONS")
                _, _, status = await self._request(method, self._url("connection_path", ""))
                result.update(ok=True, status_code=status, message="探测接口可达；鉴权及生图能力仍需执行测试图")
            else:
                await self.list_models()
                result.update(ok=True, message="模型列表读取成功；是否能生成图片仍需执行测试图")
        except Exception as exc:
            result["message"] = self._safe_error(exc)
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result

    async def test_generation(self, prompt: str = "simple blue flower on white background") -> tuple[GeneratedImage, int]:
        started = time.monotonic()
        result = await self.generate(prompt, "", options={"width": 512, "height": 512, "steps": 4, "scale": 3.0, "seed": -1})
        return result, int((time.monotonic() - started) * 1000)


class OpenAIProvider(ImageProvider):
    provider_kind = "openai"
    supported_options = {"width", "height", "size", "quality", "background", "output_format", "input_fidelity"}
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.capabilities.dimensions = "GPT Image 1 固定尺寸；GPT Image 2 及以上为 16 的倍数；默认按宽高比适配"
        self.capabilities.identity_reference = "图像编辑可携带角色参考图，身份一致性取决于模型"
        if str(config.get("model", "")).startswith(("gpt-image", "chatgpt-image")):
            self.capabilities.max_reference_images = 16
        if str(config.get("model", "")).lower() == "dall-e-3":
            self.capabilities.image_to_image = False

    def validate_config(self) -> None:
        super().validate_config()
        if self.capabilities.negative_mode == "field":
            field = self.config.get("negative_prompt_field") or "negative_prompt"
            _validate_field_paths([("negative_prompt", field)] + [(key, key) for key in ("model", "prompt", "image", "image[]", "mask", "n", "stream", "size", "quality", "background", "output_format", "input_fidelity", "response_format")])

    def _size(self, model: str, options: dict) -> str:
        requested_size = options.get("size")
        if requested_size == "auto" and model.startswith(("gpt-image", "chatgpt-image")):
            return "auto"
        if requested_size is not None:
            match = re.fullmatch(r"(\d+)x(\d+)", str(requested_size))
            if not match:
                raise ProviderError("size 必须为 WIDTHxHEIGHT 或受模型支持的 auto")
            options = {**options, "width": match[1], "height": match[2]}
        width = _integer(options.get("width", 1024), "图片宽度", 64, 4096)
        height = _integer(options.get("height", 1024), "图片高度", 64, 4096)
        explicit = bool({"width", "height", "size"} & options["_explicit"])
        if model.startswith("gpt-image-2"):
            if width % 16 or height % 16 or not 1 / 3 <= width / height <= 3 or width * height > 3840 * 2160 or max(width, height) > 3840:
                if explicit:
                    raise ProviderError("此模型尺寸需为 16 的倍数、宽高比 1:3–3:1，且不超过 3840×2160 的像素数")
                return "1024x1536" if height > width else "1536x1024" if width > height else "1024x1024"
            return f"{width}x{height}"
        sizes = self.config.get("supported_sizes") or (["1024x1024", "1792x1024", "1024x1792"] if model == "dall-e-3" else ["256x256", "512x512", "1024x1024"] if model == "dall-e-2" else ["1024x1024", "1536x1024", "1024x1536"])
        if not isinstance(sizes, list) or any(not isinstance(size, str) or not re.fullmatch(r"[1-9]\d*x[1-9]\d*", size) for size in sizes):
            raise ProviderError("supported_sizes 必须为 WIDTHxHEIGHT 字符串列表")
        requested = str(options.get("size") or f"{width}x{height}")
        if requested in sizes:
            return requested
        if explicit:
            raise ProviderError("模型不支持指定尺寸；可用尺寸：" + ", ".join(sizes))
        return min(sizes, key=lambda size: abs(math.log((int(size.split("x")[0]) / int(size.split("x")[1])) / (width / height))))

    async def generate(self, prompt: str, negative_prompt: str, *, reference: bytes | None = None, options: dict | None = None) -> GeneratedImage:
        prompt, options = self._prepare(prompt, negative_prompt, reference, options)
        model = str(self.config.get("model") or "").strip()
        if not model:
            raise ProviderError("请配置绘画模型名称")
        gpt_image = model.startswith(("gpt-image", "chatgpt-image"))
        body = {"model": model, "prompt": prompt, "n": 1, "size": self._size(model, options)}
        if not gpt_image:
            body["response_format"] = "b64_json"
        for key in ("quality", "background", "output_format", "input_fidelity"):
            if key in options:
                if key == "input_fidelity" and (reference is None or not model.startswith(("gpt-image-1", "chatgpt-image"))):
                    raise ProviderError("input_fidelity 仅支持兼容模型的图像编辑请求")
                if key in {"background", "output_format"} and not gpt_image:
                    raise ProviderError(f"此模型不支持参数：{key}")
                body[key] = options[key]
        protected = {"model", "prompt", "image", "image[]", "mask", "n", "stream"}
        negative_field = ""
        if self.capabilities.negative_mode == "field":
            negative_field = str(self.config.get("negative_prompt_field") or "negative_prompt")
            _path_set(body, negative_field, str(negative_prompt or ""))
            protected.add(negative_field)
        body = self._merge_extra(body, protected)
        if gpt_image and "response_format" in body:
            raise ProviderError("GPT Image 不支持 response_format，请从 extra_body 删除它")
        actual_options = {key: body[key] for key in ("size", "quality", "background", "output_format", "input_fidelity") if key in body}
        size = re.fullmatch(r"(\d+)x(\d+)", str(body.get("size", "")))
        if size:
            actual_options.update(width=int(size[1]), height=int(size[2]))
        candidate_field = str(self.config.get("negative_prompt_field") or "negative_prompt")
        if not negative_field and _path_get(body, candidate_field) is not None:
            negative_field = candidate_field
        if reference is not None:
            refs = reference if isinstance(reference, list) else [reference]
            for data in refs:
                extension, _, size = _image_info(data)
                if extension not in {"png", "jpg", "webp"}:
                    raise ProviderError("OpenAI 编辑参考图仅支持 PNG、JPEG、WebP")
                if model == "dall-e-2" and (extension != "png" or size[0] != size[1] or len(data) >= 4 * 1024 * 1024):
                    raise ProviderError("旧 DALL·E 2 编辑需要小于 4 MB 的正方形 PNG")
            files = {"image[]": refs} if len(refs) > 1 else {"image": refs[0]}
            self._record_request(body["prompt"], _path_get(body, negative_field) if negative_field else "", body["model"], actual_options, reference, negative_field=negative_field)
            raw, mime = await self._post(self._url("edit_path", "/v1/images/edits"), body, files=files)
        else:
            self._record_request(body["prompt"], _path_get(body, negative_field) if negative_field else "", body["model"], actual_options, reference, negative_field=negative_field)
            raw, mime = await self._post(self._url("generation_path", "/v1/images/generations"), body)
        return await self._decode_response(raw, mime, model)


class GeminiProvider(ImageProvider):
    provider_kind = "gemini"
    supported_options = {"width", "height", "size", "aspect_ratio", "image_size"}
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.capabilities.dimensions = "以 imageConfig.aspectRatio 指定宽高比；分辨率取决于模型，非任意像素尺寸"
        self.capabilities.identity_reference = "inlineData 图像参考，不保证固定身份"
        self.capabilities.max_reference_images = 14 if str(config.get("model", "")).startswith(("gemini-3-pro-image", "gemini-3.1-flash-image")) else 3

    def _headers(self) -> dict[str, str]:
        if str(self.config.get("auth_header") or "").strip():
            return super()._headers()
        result = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.config.get("api_key"):
            result["x-goog-api-key"] = str(self.config["api_key"])
        return result

    async def generate(self, prompt: str, negative_prompt: str, *, reference: bytes | None = None, options: dict | None = None) -> GeneratedImage:
        prompt, options = self._prepare(prompt, negative_prompt, reference, options)
        model = str(self.config.get("model") or "").removeprefix("models/").strip()
        if not model:
            raise ProviderError("请配置 Gemini 绘画模型")
        parts = [{"text": prompt}]
        if reference is not None:
            for data in reference if isinstance(reference, list) else [reference]:
                _, mime, _ = _image_info(data)
                parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}})
        config = {"responseModalities": ["TEXT", "IMAGE"]}
        ratios = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
        if {"width", "height", "size"} & options["_explicit"]:
            raise ProviderError("Gemini 不支持指定像素宽高，请改用 aspect_ratio 和模型支持的 image_size")
        if "aspect_ratio" in options:
            if str(options["aspect_ratio"]) not in ratios:
                raise ProviderError("Gemini 宽高比不受支持")
            config["imageConfig"] = {"aspectRatio": str(options["aspect_ratio"])}
        elif options.get("width") and options.get("height"):
            width = _integer(options["width"], "图片宽度", 64, 4096)
            height = _integer(options["height"], "图片高度", 64, 4096)
            ratio = min(ratios, key=lambda item: abs(math.log((int(item.split(":")[0]) / int(item.split(":")[1])) / (width / height))))
            config["imageConfig"] = {"aspectRatio": ratio}
        if "image_size" in options:
            if str(options["image_size"]) not in {"512", "1K", "2K", "4K"}:
                raise ProviderError("Gemini image_size 不受支持")
            config.setdefault("imageConfig", {})["imageSize"] = str(options["image_size"])
        body = self._merge_extra({"contents": [{"role": "user", "parts": parts}], "generationConfig": config}, {"contents", "generationConfig.responseModalities"})
        if len(json.dumps(body).encode("utf-8")) > 20 * 1024 * 1024:
            raise ProviderError("Gemini 图文请求超过 20 MB，请减小参考图大小")
        actual_options = {}
        for key, path in (("aspect_ratio", "generationConfig.imageConfig.aspectRatio"), ("image_size", "generationConfig.imageConfig.imageSize")):
            value = _path_get(body, path)
            if value is not None:
                actual_options[key] = value
        negative_field = str(self.config.get("negative_prompt_field") or "negative_prompt")
        if _path_get(body, negative_field) is None:
            negative_field = ""
        self._record_request(prompt, _path_get(body, negative_field) if negative_field else "", model, actual_options, reference, negative_field=negative_field)
        raw, _ = await self._post(self._url("generation_path", f"/models/{quote(model, safe='-._')}:generateContent"), body)
        payload = self._json(raw)
        candidates = payload.get("candidates", []) if isinstance(payload, dict) else []
        if not isinstance(candidates, list):
            raise ProviderError("Gemini candidates 响应格式错误")
        for candidate in candidates:
            parts = _path_get(candidate, "content.parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict) or part.get("thought"):
                    continue
                inline = part.get("inlineData", part.get("inline_data"))
                if isinstance(inline, dict) and inline.get("data"):
                    data, extension = _image_bytes(inline["data"])
                    declared = str(inline.get("mimeType", inline.get("mime_type", ""))).replace("image/jpg", "image/jpeg")
                    if declared and declared != _image_info(data)[1]:
                        raise ProviderError("Gemini 图片 MIME 与实际内容不匹配")
                    return GeneratedImage(data, extension, self.name, model)
        raise ProviderError("Gemini 没有返回图片；请检查模型是否支持图片输出及服务端拦截结果")

    async def list_models(self) -> dict:
        raw, _, _ = await self._request("GET", self._url("models_path", "/models"))
        items = self._json(raw)
        items = items.get("models") if isinstance(items, dict) else None
        if not isinstance(items, list):
            raise ProviderError("Gemini 模型列表响应格式错误")
        models = [str(item["name"]).removeprefix("models/") for item in items if isinstance(item, dict) and item.get("name") and (not item.get("supportedGenerationMethods") or "generateContent" in item["supportedGenerationMethods"])]
        return {"models": sorted(set(models)), "automatic": True, "note": "列表仅表示支持 generateContent，图片输出能力需单独测试"}


class NovelAIProvider(ImageProvider):
    provider_kind = "novelai"
    supported_options = {"width", "height", "steps", "scale", "sampler", "seed", "strength", "noise"}
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.capabilities = ProviderCapabilities(image_to_image=bool(config.get("supports_image_edit")), negative_prompt=True, negative_mode="field", seed=True, sampler=True, dimensions="宽高为 64 的倍数，支持范围取决于模型和账户", identity_reference="当前仅提供 img2img；未实现 Character Reference 或 Vibe Transfer", negative_prompt_mode="原生 negative_prompt / v4_negative_prompt", automated_generation=False)

    async def generate(self, prompt: str, negative_prompt: str, *, reference: bytes | None = None, options: dict | None = None) -> GeneratedImage:
        prompt, options = self._prepare(prompt, negative_prompt, reference, options)
        if isinstance(reference, list):
            reference = reference[0]
        width = _integer(options.get("width", 832), "图片宽度", 64, 4096)
        height = _integer(options.get("height", 1216), "图片高度", 64, 4096)
        if width % 64 or height % 64:
            raise ProviderError("NovelAI 宽高必须为 64 的倍数")
        if reference is None and {"strength", "noise"} & options["_explicit"]:
            raise ProviderError("strength / noise 仅用于携带参考图的图生图")
        model = str(self.config.get("model") or "nai-diffusion-4-5-full")
        seed = _integer(options.get("seed", -1), "随机种子", -1, 2 ** 32 - 1)
        seed = secrets.randbelow(2 ** 32) if seed == -1 else seed
        params = {"width": width, "height": height, "steps": _integer(options.get("steps", 28), "采样步数", 1, 100), "scale": _number(options.get("scale", 5), "提示词引导值", 0, 100), "sampler": str(options.get("sampler") or "k_euler_ancestral"), "n_samples": 1, "negative_prompt": str(negative_prompt or ""), "seed": seed}
        protected = {"input", "model", "action", "parameters.image", "parameters.negative_prompt", "parameters.n_samples", "parameters.stream"}
        if "nai-diffusion-4" in model or "nai-diffusion-5" in model:
            params.update(params_version=_integer(self.config.get("params_version", 3), "NovelAI 参数版本", 1, 10), v4_prompt={"caption": {"base_caption": prompt, "char_captions": []}, "use_coords": False, "use_order": True}, v4_negative_prompt={"caption": {"base_caption": str(negative_prompt or ""), "char_captions": []}, "legacy_uc": False})
            protected |= {"parameters.v4_prompt.caption", "parameters.v4_negative_prompt.caption"}
        if reference is not None:
            from PIL import Image
            # The native binary field has no MIME field; normalize input to PNG.
            with Image.open(io.BytesIO(reference)) as picture:
                buffer = io.BytesIO()
                picture.convert("RGB").save(buffer, format="PNG")
            _image_info(buffer.getvalue())
            params.update(image=base64.b64encode(buffer.getvalue()).decode("ascii"), strength=_number(options.get("strength", self.config.get("strength", 0.6)), "图生图强度", 0, 1), noise=_number(options.get("noise", self.config.get("noise", 0)), "图生图噪声", 0, 1), extra_noise_seed=seed)
        body = self._merge_extra({"input": prompt, "model": model, "action": "img2img" if reference is not None else "generate", "parameters": params}, protected)
        self._record_request(body["input"], body["parameters"]["negative_prompt"], body["model"], body["parameters"], reference, negative_field="parameters.negative_prompt", notes=["同时发送 v4_negative_prompt.caption.base_caption"] if "v4_negative_prompt" in body["parameters"] else None)
        raw, mime = await self._post(self._url("generation_path", "/ai/generate-image"), body)
        return await self._decode_response(raw, mime, model)

    async def list_models(self) -> dict:
        if self.config.get("models_path"):
            return await super().list_models()
        model = str(self.config.get("model") or "nai-diffusion-4-5-full")
        return {"models": [model], "automatic": False, "note": "NovelAI 没有统一的模型列表接口；此项仅为已配置模型，未验证可用性"}

    async def test_connection(self) -> dict:
        # OPTIONS/CORS success does not establish authenticated generation.
        self._url("generation_path", "/ai/generate-image")
        return {"ok": False, "provider": self.name, "model": self.config.get("model", ""), "elapsed_ms": 0, "requires_generation_test": True, "message": "NovelAI 无无费用的统一鉴权探测接口，请手动执行测试图验证"}


class CustomProvider(ImageProvider):
    provider_kind = "custom"
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.capabilities.seed = bool(config.get("supports_seed", False))
        self.capabilities.sampler = bool(config.get("supports_sampler", False))
        self.capabilities.dimensions = "通过 option_fields 映射自定义宽高字段"
        self.capabilities.negative_prompt_mode = "自定义独立负面字段" if self.capabilities.negative_prompt else self.capabilities.negative_prompt_mode
        fields = config.get("option_fields", {"width": "width", "height": "height", "seed": "seed", "steps": "steps", "scale": "scale", "sampler": "sampler"})
        self.supported_options = set(fields) if isinstance(fields, dict) else set()

    def validate_config(self) -> None:
        super().validate_config()
        fields = self.config.get("option_fields", {"width": "width", "height": "height", "seed": "seed", "steps": "steps", "scale": "scale", "sampler": "sampler"})
        if not isinstance(fields, dict):
            raise ProviderError("option_fields 必须为参数到请求字段的 JSON 映射")
        paths = [("prompt", self.config.get("prompt_field") or "prompt"), ("model", self.config.get("model_field") or "model"), ("reference", self.config.get("reference_field") or "image")]
        if self.capabilities.negative_mode == "field":
            paths.append(("negative_prompt", self.config.get("negative_prompt_field") or "negative_prompt"))
        if self.config.get("reference_mime_field"):
            paths.append(("reference_mime", self.config["reference_mime_field"]))
        paths.extend((f"option_fields.{key}", path) for key, path in fields.items() if path is not None and path != "")
        _validate_field_paths(paths)
        if str(self.config.get("reference_format") or "data_url") not in {"base64", "data_url"}:
            raise ProviderError("reference_format 仅支持 base64 或 data_url")

    async def generate(self, prompt: str, negative_prompt: str, *, reference: bytes | None = None, options: dict | None = None) -> GeneratedImage:
        prompt, options = self._prepare(prompt, negative_prompt, reference, options)
        if isinstance(reference, list):
            reference = reference[0]
        model = str(self.config.get("model") or "")
        body: dict = {}
        prompt_field = str(self.config.get("prompt_field") or "prompt")
        model_field = str(self.config.get("model_field") or "model")
        protected: set[str] = set()
        def reserve(field: str):
            if not field or field in protected or any(field.startswith(item + ".") or item.startswith(field + ".") for item in protected):
                raise ProviderError("自定义提示词、模型、负面词和参考图字段相互冲突")
            protected.add(field)
        reserve(prompt_field)
        reserve(model_field)
        _path_set(body, prompt_field, prompt)
        _path_set(body, model_field, model)
        if self.capabilities.negative_prompt:
            field = str(self.config.get("negative_prompt_field") or "negative_prompt")
            reserve(field)
            _path_set(body, field, str(negative_prompt or ""))
        reference_field = str(self.config.get("reference_field") or "image")
        if reference is not None:
            reserve(reference_field)
            if self.config.get("reference_mime_field"):
                reserve(str(self.config["reference_mime_field"]))
        fields = self.config.get("option_fields", {"width": "width", "height": "height", "seed": "seed", "steps": "steps", "scale": "scale", "sampler": "sampler"})
        if not isinstance(fields, dict):
            raise ProviderError("option_fields 必须为参数到请求字段的 JSON 映射")
        for key in options["_explicit"]:
            if key in options and (key not in fields or not fields[key]):
                raise ProviderError(f"自定义接口未配置参数字段：{key}")
        for key, field in fields.items():
            if key not in options or not field or key == "seed" and not self.capabilities.seed or key in {"steps", "scale", "sampler"} and not self.capabilities.sampler:
                continue
            field = str(field)
            if field in protected or any(field.startswith(item + ".") or item.startswith(field + ".") for item in protected):
                raise ProviderError("自定义参数不能覆盖提示词或图片字段")
            value = options[key]
            if key in {"width", "height"}:
                value = _integer(value, "图片尺寸", 64, 4096)
            elif key == "seed":
                value = _integer(value, "随机种子", -1, 2 ** 32 - 1)
            elif key == "steps":
                value = _integer(value, "采样步数", 1, 1000)
            elif key == "scale":
                value = _number(value, "提示词引导值", 0, 1000)
            _path_set(body, field, value)
        if reference is not None:
            _, mime, _ = _image_info(reference)
            field = reference_field
            encoded = base64.b64encode(reference).decode("ascii")
            fmt = str(self.config.get("reference_format") or "data_url")
            if fmt not in {"base64", "data_url"}:
                raise ProviderError("reference_format 仅支持 base64 或 data_url")
            _path_set(body, field, f"data:{mime};base64,{encoded}" if fmt == "data_url" else encoded)
            if self.config.get("reference_mime_field"):
                field = str(self.config["reference_mime_field"])
                _path_set(body, field, mime)
        body = self._merge_extra(body, protected | {"stream"})
        actual_options = {key: _path_get(body, str(field)) for key, field in fields.items() if field and _path_get(body, str(field)) is not None}
        negative_field = str(self.config.get("negative_prompt_field") or "negative_prompt")
        if _path_get(body, negative_field) is None:
            negative_field = ""
        self._record_request(_path_get(body, prompt_field), _path_get(body, negative_field) if negative_field else "", str(_path_get(body, model_field)), actual_options, reference, negative_field=negative_field)
        raw, mime = await self._post(self._url("edit_path" if reference is not None else "generation_path", ""), body)
        return await self._decode_response(raw, mime, model)


def provider_from_config(name: str, config: dict[str, Any]) -> ImageProvider:
    if not isinstance(config, dict):
        raise ProviderError("绘画接口配置必须为对象")
    kind = str(config.get("kind") or "openai").strip().lower()
    adapters = {"openai": OpenAIProvider, "gemini": GeminiProvider, "novelai": NovelAIProvider, "custom": CustomProvider}
    if kind not in adapters:
        raise ProviderError(f"不支持的绘画接口类型：{kind}")
    return adapters[kind](name, config)
