"""ComfyUI API-format workflows, scoped queue cancellation, and local output reads.

The workflow remains authoritative for checkpoints and unmapped inputs. Mapping
paths address existing scalar inputs only; graph links are never overwritten.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import quote, urlencode

from .base import (
    GeneratedImage, ImageProvider, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, ProviderCapabilities,
    ProviderError, _image_info, _integer, _number, _path_get, _path_set,
    _validate_field_paths,
)

WORKFLOW_FIELDS = {"prompt", "negative_prompt", "reference", "width", "height", "seed", "steps", "scale", "sampler"}
OPTION_FIELDS = WORKFLOW_FIELDS - {"prompt", "negative_prompt", "reference"}
MAX_WORKFLOW_BYTES = 2 * 1024 * 1024
# Explicit equivalents verified against comfy/samplers.py. Shared defaults use
# NovelAI-style names; deliberate per-request values remain authoritative.
SHARED_SAMPLER_ALIASES = {"k_" + name: name for name in (
    "euler", "euler_ancestral", "dpm_2", "dpm_2_ancestral", "lms", "heun",
    "dpm_fast", "dpm_adaptive", "dpmpp_2s_ancestral", "dpmpp_sde", "dpmpp_2m",
    "dpmpp_2m_sde", "dpmpp_3m_sde",
)}


class ComfyUIProvider(ImageProvider):
    provider_kind = "comfyui"
    poll_interval = 0.5

    def __init__(self, name: str, config: dict[str, Any]):
        derived = copy.deepcopy(config)
        fields = config.get("workflow_fields")
        fields = fields if isinstance(fields, dict) else {}
        derived["negative_mode"] = "field" if fields.get("negative_prompt") else "disabled"
        super().__init__(name, derived)
        self.supported_options = {key for key in OPTION_FIELDS if fields.get(key)}
        self.on_progress = None
        self.last_recovery = {}
        self.capabilities = ProviderCapabilities(
            image_to_image=bool(fields.get("reference")),
            negative_prompt=bool(fields.get("negative_prompt")),
            seed=bool(fields.get("seed")),
            sampler=bool(self.supported_options & {"steps", "scale", "sampler"}),
            dimensions="仅写入 workflow_fields 已映射的宽高；其余沿用工作流",
            identity_reference="通过上传图片与工作流参考节点保持外观，效果取决于工作流",
            negative_mode=derived["negative_mode"],
            negative_prompt_mode=("工作流独立负面输入：" + str(fields["negative_prompt"])) if fields.get("negative_prompt") else "未映射插件负面词，沿用工作流原有输入",
            max_reference_images=1,
        )

    def _comfy_url(self, route: str) -> str:
        # Only plugin-selected routes on the configured endpoint are allowed.
        self._url("_comfy_fixed_route", "")
        endpoint = str(self.config.get("endpoint") or "").strip().rstrip("/")
        self._origin(endpoint)  # Validate ports even when no route override exists.
        return self._endpoint_path(endpoint, route)

    def validate_config(self) -> None:
        super().validate_config()
        self._comfy_url("")
        if self.config.get("_comfy_fixed_route"):
            raise ProviderError("ComfyUI 不支持覆盖内部固定路由")
        if self.config.get("extra_body"):
            raise ProviderError("ComfyUI 请直接编辑 API 工作流，不支持 extra_body 覆盖")
        workflow = self.config.get("workflow")
        if not isinstance(workflow, dict) or not workflow or "nodes" in workflow or "links" in workflow:
            raise ProviderError("ComfyUI workflow 必须为 API 格式 JSON 对象，请导出 API 工作流而非画布 JSON")
        try:
            encoded = json.dumps(workflow, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProviderError("ComfyUI 工作流必须为有效 JSON") from exc
        if len(encoded) > MAX_WORKFLOW_BYTES or len(workflow) > 2000:
            raise ProviderError("ComfyUI 工作流超过 2 MB 或 2000 个节点")
        for node_id, node in workflow.items():
            if not isinstance(node_id, str) or not node_id or not isinstance(node, dict) or not isinstance(node.get("class_type"), str) or not node["class_type"].strip() or not isinstance(node.get("inputs"), dict):
                raise ProviderError("ComfyUI API 工作流每个节点必须包含 class_type 与 inputs")
        fields = self.config.get("workflow_fields")
        if not isinstance(fields, dict) or not fields.get("prompt"):
            raise ProviderError("ComfyUI workflow_fields 必须映射 prompt 到真实节点输入")
        unknown = set(fields) - WORKFLOW_FIELDS
        if unknown:
            raise ProviderError("ComfyUI workflow_fields 包含不支持的参数")
        active = [(key, value) for key, value in fields.items() if value not in ("", None)]
        _validate_field_paths(active)
        for key, path in active:
            match = re.fullmatch(r"([^.]+)\.inputs\.([^.]+)", path)
            if not match:
                raise ProviderError(f"ComfyUI 映射必须为 <节点>.inputs.<字段>：{key}")
            node = workflow.get(match[1])
            if not isinstance(node, dict) or match[2] not in node["inputs"]:
                raise ProviderError(f"ComfyUI 映射指向不存在的节点输入：{key}")
            value = node["inputs"][match[2]]
            if isinstance(value, (list, dict)) or value is None:
                raise ProviderError(f"ComfyUI 映射不能覆盖节点连接或对象：{key}")
            if key in {"prompt", "negative_prompt", "reference", "sampler"} and not isinstance(value, str):
                raise ProviderError(f"ComfyUI 文本或参考图映射必须指向字符串输入：{key}")
            if key in {"seed", "steps", "width", "height", "scale"} and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ProviderError(f"ComfyUI 数值映射必须指向数字输入：{key}")
        output = self.config.get("output_node", "")
        if not isinstance(output, str) or output and output not in workflow:
            raise ProviderError("ComfyUI output_node 必须为空或工作流中的真实节点 ID")

    @staticmethod
    def _file_descriptor(value: Any, *, upload: bool = False) -> tuple[str, str, str]:
        if not isinstance(value, dict):
            raise ProviderError("ComfyUI 图片文件描述无效")
        name = value.get("name" if upload else "filename")
        folder = value.get("subfolder", "")
        kind = value.get("type", "input" if upload else "output")
        if not isinstance(name, str) or not name or len(name) > 512 or name in {".", ".."} or any(char in name for char in ("/", "\\", "\0", ":")) or ".." in name:
            raise ProviderError("ComfyUI 返回了无效图片文件名")
        if not isinstance(folder, str) or len(folder) > 1024 or "\\" in folder or "\0" in folder or ":" in folder or folder.startswith("/") or any(part in {".", ".."} for part in folder.split("/")):
            raise ProviderError("ComfyUI 返回了无效图片子目录")
        if kind not in ({"input"} if upload else {"output", "temp"}):
            raise ProviderError("ComfyUI 返回了不受支持的图片目录类型")
        return name, folder, kind

    def _comfy_json(self, raw: bytes, operation: str) -> dict:
        try:
            value = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProviderError(f"ComfyUI {operation}没有返回有效 JSON") from exc
        if not isinstance(value, dict):
            raise ProviderError(f"ComfyUI {operation}响应必须为对象")
        if value.get("error"):
            # Node errors often echo complete inputs and third-party credentials.
            raise ProviderError(f"ComfyUI {operation}失败，请检查工作流节点、模型及映射配置")
        return value

    async def _json_request(self, method: str, route: str, body: dict | None = None, *, files: dict | None = None, operation: str) -> dict:
        raw, _, _ = await self._http(method, self._comfy_url(route), body, files=files, limit=8 * 1024 * 1024)
        return self._comfy_json(raw, operation)

    async def _cancel_queued(self, prompt_id: str) -> None:
        if not prompt_id:
            return
        try:
            await asyncio.wait_for(self._http("POST", self._comfy_url("/queue"), {"delete": [prompt_id]}, limit=1024 * 1024), timeout=2)
        except (Exception, asyncio.CancelledError):
            # Older ComfyUI versions only remove queued work; never interrupt all.
            pass

    def recovery_fingerprint(self) -> str:
        """Bind a saved prompt without persisting the source configuration.

        Only a digest is persisted; the workflow can contain private node inputs.
        Authentication rotation and timeout changes do not change the binding.
        """
        value = {
            "version": 1, "provider": self.name,
            "endpoint": str(self.config.get("endpoint") or "").strip().rstrip("/"),
            "workflow": self.config.get("workflow"),
            "fields": self.config.get("workflow_fields"),
            "output_node": self.config.get("output_node", ""),
        }
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _prompt_id(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ProviderError("ComfyUI 任务编号无效")
        return value

    async def _progress(self, phase: str, prompt_id: str = "") -> None:
        record = {
            "kind": "comfyui", "provider": self.name, "prompt_id": prompt_id,
            "fingerprint": self.recovery_fingerprint(), "phase": phase,
            "submitted": True, "upstream_accepted": bool(prompt_id),
        }
        self.last_recovery = record
        self.last_request.update(submitted=True, upstream_accepted=bool(prompt_id), recovery=copy.deepcopy(record))
        if prompt_id:
            self.last_request["workflow_prompt_id"] = prompt_id
        callback = self.on_progress
        if callable(callback):
            result = callback(copy.deepcopy(record))
            if inspect.isawaitable(result):
                await result

    async def _history_image(self, prompt_id: str) -> dict | None:
        payload = await self._json_request("GET", "/history/" + quote(prompt_id, safe=""), operation="读取历史")
        history = payload.get(prompt_id)
        if history is not None and not isinstance(history, dict):
            raise ProviderError("ComfyUI 任务历史格式错误")
        return self._select_image(history) if isinstance(history, dict) else None

    async def _queue_phase(self, prompt_id: str) -> str:
        queue = await self._json_request("GET", "/queue", operation="读取任务队列")
        for field, phase in (("queue_running", "running"), ("queue_pending", "queued")):
            entries = queue.get(field)
            if not isinstance(entries, list):
                raise ProviderError("ComfyUI 任务队列格式错误")
            for entry in entries:
                # Queue entries also contain full graphs and private node inputs.
                # Inspect only their prompt ID; never store or return the rest.
                if not isinstance(entry, (list, tuple)) or len(entry) < 2 or not isinstance(entry[1], str):
                    raise ProviderError("ComfyUI 任务队列条目格式错误")
                if entry[1] == prompt_id:
                    return phase
        return "missing"

    async def _fetch_image(self, prompt_id: str, image: dict, model: str) -> GeneratedImage:
        name, folder, kind = self._file_descriptor(image)
        await self._progress("fetching", prompt_id)
        url = self._comfy_url("/view") + "?" + urlencode({"filename": name, "subfolder": folder, "type": kind})
        raw, mime, _ = await self._http("GET", url, limit=MAX_IMAGE_BYTES)
        extension, actual_mime, _ = _image_info(raw)
        declared = mime.split(";", 1)[0].strip().lower().replace("image/jpg", "image/jpeg")
        if declared.startswith("image/") and declared != actual_mime:
            raise ProviderError("ComfyUI 图片 MIME 与实际内容不匹配")
        if declared and declared not in {actual_mime, "application/octet-stream", "binary/octet-stream"}:
            raise ProviderError("ComfyUI 图片响应类型无效")
        await self._progress("completed", prompt_id)
        return GeneratedImage(raw, extension, self.name, model)

    async def recover(self, prompt_id: str, *, fingerprint: str) -> GeneratedImage | None:
        """Inspect one previously accepted task; never submit, cancel or send.

        None means no image was found in this check, not generation failure.
        ``last_recovery.phase`` distinguishes queued/running/missing. A missing
        task may have been removed from remote history; it cannot be resubmitted
        automatically without risking a second generation.
        """
        self.validate_config()
        prompt_id = self._prompt_id(prompt_id)
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or not secrets.compare_digest(fingerprint, self.recovery_fingerprint()):
            raise ProviderError("ComfyUI 接口或工作流已改变，请恢复原任务配置后再检查结果")
        self.last_request = {}

        async def inspect_result():
            image = await self._history_image(prompt_id)
            if image is not None:
                return await self._fetch_image(prompt_id, image, str(self.config.get("model") or self.name or "ComfyUI 工作流"))
            phase = await self._queue_phase(prompt_id)
            if phase == "missing":
                # Completion can move a prompt from queue to history between
                # the two reads. Recheck once before reporting it unavailable.
                image = await self._history_image(prompt_id)
                if image is not None:
                    return await self._fetch_image(prompt_id, image, str(self.config.get("model") or self.name or "ComfyUI 工作流"))
            await self._progress(phase, prompt_id)
            return None

        try:
            return await asyncio.wait_for(inspect_result(), timeout=_integer(self.config.get("timeout", 180), "超时", 5, 600))
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ProviderError("ComfyUI 原任务查询超时，未重新提交或取消任务") from exc

    @staticmethod
    def _option(key: str, value: Any, *, randomize: bool = True) -> Any:
        if key in {"width", "height"}:
            return _integer(value, "图片尺寸", 64, 8192)
        if key == "seed":
            # ComfyUI uses uint64 seeds; avoid converting large integers to float.
            if isinstance(value, bool):
                raise ProviderError("ComfyUI 随机种子必须为 -1 或 uint64 整数")
            if isinstance(value, int) or isinstance(value, str) and re.fullmatch(r"-?\d+", value):
                number = int(value)
                if not -1 <= number <= 2 ** 64 - 1:
                    raise ProviderError("ComfyUI 随机种子超出支持范围")
            else:
                number = _integer(value, "随机种子", -1, 2 ** 64 - 1)
            return secrets.randbits(63) if randomize and number == -1 else number
        if key == "steps":
            return _integer(value, "采样步数", 1, 1000)
        if key == "scale":
            return _number(value, "提示词引导值", 0, 1000)
        if key == "sampler":
            if not isinstance(value, str) or not value.strip() or len(value) > 100:
                raise ProviderError("ComfyUI 采样器名称无效")
            return value.strip()
        return value

    def _select_image(self, history: dict) -> dict | None:
        status = history.get("status", {})
        if status is None:  # Legacy PromptQueue.task_done accepts no status.
            status = {}
        if not isinstance(status, dict):
            raise ProviderError("ComfyUI 历史状态格式错误")
        messages = status.get("messages", []) if isinstance(status, dict) else []
        if not isinstance(messages, list):
            raise ProviderError("ComfyUI 执行消息格式错误")
        failed = status.get("status_str") in {"error", "failed", "interrupted"} or any(isinstance(item, (list, tuple)) and item and item[0] in {"execution_error", "execution_interrupted"} for item in messages)
        if failed:
            raise ProviderError("ComfyUI 工作流执行失败或被中断，请检查本地节点与模型日志")
        outputs = history.get("outputs", {})
        if not isinstance(outputs, dict):
            raise ProviderError("ComfyUI 历史输出格式错误")
        output_node = self.config.get("output_node")
        candidates = [outputs.get(output_node)] if output_node else list(outputs.values())
        for output in candidates:
            images = output.get("images") if isinstance(output, dict) else None
            if isinstance(images, list) and images:
                if not isinstance(images[0], dict):
                    raise ProviderError("ComfyUI 图片输出格式错误")
                return images[0]
        if status.get("completed") is True or status.get("status_str") == "success":
            raise ProviderError("ComfyUI 已完成但没有图片输出，请检查 SaveImage/PreviewImage 与 output_node")
        return None

    def _prepare_generation(self, prompt: str, negative_prompt: str, *, reference=None, options=None, randomize: bool = False) -> dict:
        supplied = copy.deepcopy(options) if isinstance(options, dict) else options
        if isinstance(supplied, dict):
            explicit = supplied.get("_explicit", [])
            if isinstance(explicit, (list, tuple, set)):
                # Shared defaults do not turn unmapped graph inputs into errors.
                for key in OPTION_FIELDS - self.supported_options:
                    if key not in explicit:
                        supplied.pop(key, None)
        prompt, options = self._prepare(prompt, negative_prompt, reference, supplied)
        workflow = copy.deepcopy(self.config["workflow"])
        fields = self.config["workflow_fields"]
        _path_set(workflow, fields["prompt"], prompt)
        if fields.get("negative_prompt"):
            _path_set(workflow, fields["negative_prompt"], str(negative_prompt or ""))
        notes = ["模型与未映射输入沿用 API 工作流；取消仅移除本次排队任务，已经运行的任务可能继续"]
        for key in self.supported_options:
            if key in options:
                value = self._option(key, options[key], randomize=randomize)
                if key == "sampler" and key not in options["_explicit"] and value.startswith("k_"):
                    adapted = SHARED_SAMPLER_ALIASES.get(value)
                    if adapted is None:
                        notes.append("共享采样器默认名称未确认兼容性，沿用工作流采样器；可通过显式参数指定自定义名称")
                        continue
                    notes.append(f"共享采样器默认值已适配为 ComfyUI 名称：{value} → {adapted}")
                    value = adapted
                _path_set(workflow, fields[key], value)
        actual_options = {key: _path_get(workflow, fields[key]) for key in self.supported_options}
        if {"width", "height"} <= set(actual_options):
            width = _integer(actual_options["width"], "图片宽度", 1, 8192)
            height = _integer(actual_options["height"], "图片高度", 1, 8192)
            if width * height > MAX_IMAGE_PIXELS:
                raise ProviderError("ComfyUI 映射的图片尺寸超过 3600 万像素限制")
        negative_field = str(fields.get("negative_prompt") or "")
        model = str(self.config.get("model") or self.name or "ComfyUI 工作流")
        return {"workflow": workflow, "fields": fields, "prompt": prompt, "model": model, "options": actual_options, "negative_field": negative_field, "notes": notes}

    async def generate(self, prompt: str, negative_prompt: str, *, reference: bytes | None = None, options: dict[str, Any] | None = None) -> GeneratedImage:
        self.last_request = {}
        self.last_recovery = {}
        prepared = self._prepare_generation(prompt, negative_prompt, reference=reference, options=options, randomize=True)
        workflow, fields, model = prepared["workflow"], prepared["fields"], prepared["model"]
        negative_field = prepared["negative_field"]
        self._record_request(prepared["prompt"], _path_get(workflow, negative_field) if negative_field else "", model, prepared["options"], reference, negative_field=negative_field, notes=prepared["notes"])
        self.last_request["submitted"] = False
        self.last_request["output_node"] = str(self.config.get("output_node") or "")
        state = {"prompt_id": ""}

        async def run() -> GeneratedImage:
            if reference is not None:
                data = reference[0] if isinstance(reference, list) else reference
                uploaded = await self._json_request("POST", "/upload/image", {"type": "input", "subfolder": "persona_canvas", "overwrite": "false"}, files={"image": data}, operation="上传参考图")
                name, folder, _ = self._file_descriptor(uploaded, upload=True)
                _path_set(workflow, fields["reference"], f"{folder}/{name}" if folder else name)
            await self._progress("submitting")
            submitted = await self._json_request("POST", "/prompt", {"prompt": workflow, "client_id": "persona-canvas-" + secrets.token_hex(12)}, operation="提交工作流")
            prompt_id = submitted.get("prompt_id")
            if not isinstance(prompt_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prompt_id):
                raise ProviderError("ComfyUI 提交响应缺少有效 prompt_id")
            state["prompt_id"] = prompt_id
            await self._progress("accepted", prompt_id)
            checked_queue_at = 0.0
            while True:
                image = await self._history_image(prompt_id)
                if image is not None:
                    return await self._fetch_image(prompt_id, image, model)
                if callable(self.on_progress) and time.monotonic() - checked_queue_at >= 2:
                    checked_queue_at = time.monotonic()
                    try:
                        phase = await self._queue_phase(prompt_id)
                    except ProviderError:
                        phase = "accepted"  # History polling remains authoritative.
                    if phase in {"running", "queued"} and self.last_recovery.get("phase") != phase:
                        await self._progress(phase, prompt_id)
                await asyncio.sleep(self.poll_interval)

        try:
            return await asyncio.wait_for(run(), timeout=_integer(self.config.get("timeout", 180), "超时", 5, 600))
        except asyncio.CancelledError:
            await self._cancel_queued(state["prompt_id"])
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            await self._cancel_queued(state["prompt_id"])
            raise ProviderError("ComfyUI 等待超时；已尝试移除本次排队任务，已经运行的任务可能继续") from exc

    async def list_models(self) -> dict[str, Any]:
        self.validate_config()
        return {"models": [str(self.config.get("model") or self.name)], "automatic": False, "note": "ComfyUI 模型由工作流节点决定；此名称仅用于标记工作流"}

    async def test_connection(self) -> dict[str, Any]:
        started = time.monotonic()
        result = {"ok": False, "provider": self.name, "model": str(self.config.get("model") or self.name)}
        try:
            self.validate_config()
            nodes = await self._json_request("GET", "/object_info", operation="读取节点信息")
            required = {node["class_type"] for node in self.config["workflow"].values()}
            if required - set(nodes):
                raise ProviderError("ComfyUI 缺少工作流需要的节点，请安装相应自定义节点后重试")
            result.update(ok=True, status_code=200, message="ComfyUI 可达且工作流节点齐全；模型文件与生成效果仍需执行测试图")
        except Exception as exc:
            result["message"] = self._safe_error(exc)
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result
