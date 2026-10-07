from __future__ import annotations

import base64
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin


@dataclass
class GeneratedImage:
    data: bytes
    extension: str = "png"
    provider: str = ""
    model: str = ""


class ProviderError(RuntimeError):
    pass


@dataclass
class ProviderCapabilities:
    text_to_image: bool = True
    image_to_image: bool = False
    negative_prompt: bool = True
    seed: bool = False


def _path_get(value: Any, path: str) -> Any:
    if not path:
        return None
    for part in path.split("."):
        if isinstance(value, list) and part.isdigit():
            value = value[int(part)] if int(part) < len(value) else None
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _image_bytes(value: str) -> tuple[bytes, str]:
    if value.startswith("data:image/"):
        header, encoded = value.split(",", 1)
        content_type = header[5:].split(";", 1)[0]
        return base64.b64decode(encoded), {"jpeg": "jpg", "jpg": "jpg", "webp": "webp"}.get(content_type, "png")
    return base64.b64decode(value), "png"


class ImageProvider:
    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self.capabilities = ProviderCapabilities(
            image_to_image=bool(config.get("supports_image_edit")),
            negative_prompt=bool(config.get("negative_prompt", True)),
            seed=bool(config.get("supports_seed")),
        )

    def _headers(self) -> dict[str, str]:
        key = str(self.config.get("api_key") or "")
        header = str(self.config.get("auth_header") or "Authorization")
        prefix = str(self.config.get("auth_prefix", "Bearer "))
        result = {"Accept": "application/json", "Content-Type": "application/json"}
        if key:
            result[header] = f"{prefix}{key}"
        return result

    async def _post(self, url: str, body: dict[str, Any], *, files: dict[str, bytes] | None = None) -> tuple[bytes, str]:
        try:
            import aiohttp
        except ImportError as exc:
            raise ProviderError("AstrBot 环境缺少 aiohttp") from exc
        timeout = aiohttp.ClientTimeout(total=max(10, min(600, int(self.config.get("timeout", 180)))))
        if files:
            data = aiohttp.FormData()
            for key, value in body.items():
                data.add_field(key, json.dumps(value) if isinstance(value, (dict, list)) else str(value))
            for field, content in files.items():
                data.add_field(field, content, filename="reference.png", content_type="image/png")
            headers = self._headers()
            headers.pop("Content-Type", None)
            request_body = data
        else:
            headers = self._headers()
            request_body = json.dumps(body, ensure_ascii=False)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, data=request_body, allow_redirects=False) as response:
                    raw = await response.read()
                    if len(raw) > 64 * 1024 * 1024:
                        raise ProviderError("生图接口响应超过 64 MB")
                    if response.status >= 400:
                        brief = raw[:500].decode("utf-8", "replace")
                        hint = "请检查 Key 和模型配置" if response.status in {401, 403} else "请求被限流或服务暂不可用" if response.status == 429 else "请检查接口地址和参数"
                        raise ProviderError(f"HTTP {response.status}：{hint}。{brief}")
                    return raw, response.headers.get("Content-Type", "")
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"生图请求失败：{exc}") from exc

    async def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> tuple[bytes, str, int]:
        try:
            import aiohttp
        except ImportError as exc:
            raise ProviderError("AstrBot 环境缺少 aiohttp") from exc
        timeout = aiohttp.ClientTimeout(total=max(5, min(120, int(self.config.get("timeout", 30)))))
        headers = self._headers()
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=headers, json=body, allow_redirects=False) as response:
                    raw = await response.content.read(8 * 1024 * 1024 + 1)
                    if len(raw) > 8 * 1024 * 1024:
                        raise ProviderError("模型接口响应超过 8 MB")
                    if response.status >= 400:
                        brief = raw[:400].decode("utf-8", "replace")
                        raise ProviderError(f"HTTP {response.status}：{brief}")
                    return raw, response.headers.get("Content-Type", ""), response.status
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"模型接口请求失败：{exc}") from exc

    async def list_models(self) -> dict[str, Any]:
        kind = str(self.config.get("kind", "openai")).lower()
        endpoint = str(self.config.get("endpoint") or "").rstrip("/")
        if kind == "novelai":
            current = str(self.config.get("model") or "nai-diffusion-4-5-full")
            choices = list(dict.fromkeys([current, "nai-diffusion-4-5-full", "nai-diffusion-4-5-curated", "nai-diffusion-4-5-full-inpainting", "nai-diffusion-4-5-curated-inpainting", "nai-diffusion-4-full", "nai-diffusion-3"]))
            return {"models": choices, "automatic": False, "note": "NovelAI 各代理接口没有统一的模型列表端点；列表为常见模型建议。"}
        if not endpoint:
            raise ProviderError("请先填写接口地址")
        if kind == "gemini":
            url = endpoint if endpoint.endswith("/models") else f"{endpoint}/models"
        else:
            url = self._endpoint_path(endpoint, "/v1/models")
        raw, _, _ = await self._request("GET", url)
        payload = json.loads(raw.decode("utf-8"))
        if kind == "gemini":
            models = payload.get("models", [])
            values = [str(item.get("name", "")).removeprefix("models/") for item in models if isinstance(item, dict) and (not item.get("supportedGenerationMethods") or "generateContent" in item.get("supportedGenerationMethods", []))]
        else:
            values = [str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]
        return {"models": sorted(set(value for value in values if value)), "automatic": True, "note": ""}

    async def test_connection(self) -> dict[str, Any]:
        started = time.monotonic()
        kind = str(self.config.get("kind", "openai")).lower()
        endpoint = str(self.config.get("endpoint") or "").rstrip("/")
        if not endpoint:
            raise ProviderError("请先填写接口地址")
        if kind == "novelai":
            url = f"{endpoint}/ai/generate-image"
            method = "OPTIONS"
        elif kind == "gemini":
            url = endpoint if endpoint.endswith("/models") else f"{endpoint}/models"
            method = "GET"
        else:
            url = self._endpoint_path(endpoint, "/v1/models")
            method = "GET"
        try:
            raw, _, status = await self._request(method, url)
            if method == "GET":
                json.loads(raw.decode("utf-8"))
            return {"ok": True, "provider": self.name, "model": self.config.get("model", ""), "status_code": status, "elapsed_ms": int((time.monotonic() - started) * 1000), "message": "接口连接成功"}
        except Exception as exc:
            return {"ok": False, "provider": self.name, "model": self.config.get("model", ""), "elapsed_ms": int((time.monotonic() - started) * 1000), "message": str(exc)[:500]}

    async def test_generation(self, prompt: str = "simple blue flower on white background") -> tuple[GeneratedImage, int]:
        options = {"width": 512, "height": 512, "steps": 4, "scale": 3.0, "sampler": "k_euler_ancestral", "seed": -1}
        started = time.monotonic()
        result = await self.generate(prompt, "", options=options)
        return result, int((time.monotonic() - started) * 1000)


    @staticmethod
    def _endpoint_path(endpoint: str, suffix: str) -> str:
        base = endpoint.rstrip("/")
        if base.endswith("/v1") and suffix.startswith("/v1"):
            return base[:-3] + suffix
        return base + suffix

    async def _openai(self, prompt: str, negative: str, options: dict[str, Any], reference: bytes | None) -> GeneratedImage:
        endpoint = str(self.config.get("endpoint") or "").rstrip("/")
        if not endpoint:
            raise ProviderError("尚未配置 OpenAI/GPT 生图接口地址")
        model = str(self.config.get("model") or "")
        body: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1, "size": f"{int(options.get('width', 832))}x{int(options.get('height', 1216))}", "response_format": "b64_json"}
        if reference:
            if not self.capabilities.image_to_image:
                raise ProviderError("当前 OpenAI provider 未声明图生图能力")
            raw, content_type = await self._post(self._endpoint_path(endpoint, "/v1/images/edits"), body, files={"image": reference})
        else:
            raw, content_type = await self._post(self._endpoint_path(endpoint, "/v1/images/generations"), body)
        return self._decode(raw, content_type, model)

    async def _gemini(self, prompt: str, negative: str, options: dict[str, Any], reference: bytes | None) -> GeneratedImage:
        endpoint = str(self.config.get("endpoint") or "").rstrip("/")
        model = str(self.config.get("model") or "")
        if not endpoint or not model:
            raise ProviderError("尚未配置 Gemini endpoint 或模型")
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if reference:
            if not self.capabilities.image_to_image:
                raise ProviderError("当前 Gemini provider 未声明图生图能力")
            parts.insert(0, {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(reference).decode()}})
        body = {"contents": [{"parts": parts}], "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
        raw, content_type = await self._post(f"{endpoint}/models/{model}:generateContent", body)
        try:
            payload = json.loads(raw.decode("utf-8"))
            for part in payload.get("candidates", [])[0].get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return GeneratedImage(base64.b64decode(inline["data"]), "png", self.name, model)
        except (ValueError, IndexError, KeyError, TypeError):
            pass
        raise ProviderError("Gemini 响应中没有找到图片数据")

    async def _novelai(self, prompt: str, negative: str, options: dict[str, Any], reference: bytes | None) -> GeneratedImage:
        endpoint = str(self.config.get("endpoint") or "").rstrip("/")
        if not endpoint:
            raise ProviderError("尚未配置 NovelAI 接口地址")
        if reference and not self.capabilities.image_to_image:
            raise ProviderError("当前 NovelAI provider 未声明图生图能力")
        width, height = int(options.get("width", 832)), int(options.get("height", 1216))
        model = str(self.config.get("model") or "nai-diffusion-4-5-full")
        params = {"width": width, "height": height, "steps": int(options.get("steps", 28)), "scale": float(options.get("scale", 5)), "sampler": options.get("sampler", "k_euler_ancestral"), "n_samples": 1, "negative_prompt": negative, "seed": int(options.get("seed", -1))}
        if "-4" in model or "-5" in model:
            params["params_version"] = 4 if "-5" in model else 3
            params["v4_prompt"] = {"caption": {"base_caption": prompt, "char_captions": []}, "use_coords": False, "use_order": True}
            params["v4_negative_prompt"] = {"caption": {"base_caption": negative, "char_captions": []}, "legacy_uc": False}
        body = {"input": prompt, "model": model, "action": "generate", "parameters": params}
        raw, content_type = await self._post(f"{endpoint}/ai/generate-image", body)
        return self._decode(raw, content_type, model)

    def _decode(self, raw: bytes, content_type: str, model: str) -> GeneratedImage:
        if raw.startswith(b"\x89PNG"):
            return GeneratedImage(raw, "png", self.name, model)
        if raw.startswith(b"\xff\xd8"):
            return GeneratedImage(raw, "jpg", self.name, model)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("生图接口没有返回可识别的 JSON 或图片") from exc
        value = _path_get(payload, str(self.config.get("response_path") or ""))
        if value is None:
            value = ((payload.get("data") or [{}])[0].get("b64_json") if isinstance(payload.get("data"), list) else None)
        if value is None and isinstance(payload.get("data"), list):
            value = (payload["data"][0] or {}).get("url")
        if not isinstance(value, str):
            raise ProviderError("接口响应中找不到图片字段")
        if value.startswith("http"):
            raise ProviderError("暂不支持需要二次下载的图片 URL，请使用 base64 响应")
        data, ext = _image_bytes(value)
        return GeneratedImage(data, ext, self.name, model)


def provider_from_config(name: str, config: dict[str, Any]) -> ImageProvider:
    return ImageProvider(name, config)
