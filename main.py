from __future__ import annotations

import asyncio
import base64
import copy
import dataclasses
import io
import json
import math
import re
import time
from contextlib import asynccontextmanager

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from .active import ActiveScheduler
from .dialogue import Dialogue, field, maybe_await
from .delivery import delivery_guard, local_delivery_guard
from .companion import REQUIREMENT_KEYS, apply_state, cancel_request, infer_requirements, is_confirmation, pending_request, requirements, semantic_request_error, valid_pending
from .intent import Intent, message_text, photo_request, prompt_bundle, state_patch, state_request
from .moderation import Moderation
from .page_api import CanvasPageApi, image_data_url
from .providers.base import ProviderCapabilities, ProviderError, provider_from_config
from .reply_images import ReplyImages
from .storage import Storage, _secret_key
from .webui_server import WebUI

PLUGIN_NAME = "astrbot_plugin_persona_canvas"
VERSION = "0.5.16"


class DeliveryRejectedError(ValueError):
    """The platform explicitly rejected a saved image, so it may be resent."""


def _restore_workflow_secrets(value, previous):
    """Round-trip the redacted editor without deleting stored node credentials."""
    if isinstance(value, list):
        old = previous if isinstance(previous, list) else []
        return [_restore_workflow_secrets(item, old[i] if i < len(old) else None) for i, item in enumerate(value)]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    old = previous if isinstance(previous, dict) else {}
    restored = {key: _restore_workflow_secrets(item, old.get(key)) for key, item in value.items()}
    for key, secret in old.items():
        if _secret_key(key):
            marker = "has_" + str(key).lower().replace("-", "_")
            if marker in value and isinstance(value[marker], bool):
                if key not in value:
                    restored[key] = copy.deepcopy(secret)
                restored.pop(marker, None)
    return restored


def _text(event):
    return message_text(getattr(event, "message_str", ""))


def _admin(event):
    return bool(event.is_admin())


def _addressed(event):
    return bool(getattr(event, "is_at_or_wake_command", False) or event.is_private_chat())


def _extra(event, key, value=None, *, set_value=False):
    if set_value:
        event.set_extra("persona_canvas:" + key, value)
        return value
    return event.get_extra("persona_canvas:" + key, value)


@register(PLUGIN_NAME, "iownmmiku", "随想画卷：先由角色决定，再拍摄照片", VERSION)
class PersonaCanvasPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.context, self.config = context, config or {}
        self.storage = Storage()
        self.dialogue = Dialogue(context, self.storage)
        self.moderation = Moderation(self.storage.settings, self.storage)
        self.reply_images = ReplyImages(self)
        self.page_api = CanvasPageApi(self)
        self.webui = None
        self.active = None
        self._tasks = set()
        self._requests = set()
        self._job_tasks = {}
        self._session_locks = {}
        self._slots = asyncio.Condition()
        self._running = 0
        self._generation_waiters = []
        self._generation_ticket = 0
        self._closing = False
        self._apply_plugin_config()

    def _apply_plugin_config(self):
        # The embedded console owns business settings. Legacy flat fields are
        # imported once only if explicitly non-default, never on every restart.
        if "plugin_config_imported" in self.storage.runtime:
            return
        mapping = {"generation_width": ("generation", "width", 832), "generation_height": ("generation", "height", 1216), "generation_steps": ("generation", "steps", 28), "generation_scale": ("generation", "scale", 5), "generation_sampler": ("generation", "sampler", "k_euler_ancestral"), "generation_seed": ("generation", "seed", -1), "moderation_daily_limit": ("moderation", "daily_limit", 5), "moderation_min_interval_sec": ("moderation", "min_interval_sec", 20)}
        for name, (group, key, default) in mapping.items():
            if name in self.config and self.config[name] != default:
                self.storage.settings[group][key] = self.config[name]
        self.storage.runtime["plugin_config_imported"] = VERSION
        self.storage.save_all()

    def _spawn(self, coroutine, name, job_id=""):
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if job_id:
            self._bind_job_task(job_id, task)
        return task

    def _bind_job_task(self, job_id, task):
        self._job_tasks[job_id] = task
        def release(completed):
            if self._job_tasks.get(job_id) is completed:
                self._job_tasks.pop(job_id, None)
        task.add_done_callback(release)

    def _trace_job(self, job_id, stage, status="ok", detail=""):
        job = self.storage.job(job_id)
        trace = (job.get("trace") or []) + [{"at": time.time(), "stage": stage, "status": status, "detail": self._error(detail)[:1000]}]
        return self.storage.update_job(job_id, {"trace": trace[-30:]})

    def _photo_note(self, job):
        if job.get("session_key"):
            self.storage.record_action({"kind": "photo", "job_id": job["id"], "session_key": job["session_key"], "persona_id": job.get("persona_id"), "umo": job.get("umo"), "status": job["status"], "summary": job.get("request_prompt") or job.get("raw", ""), "visual_state": {**job.get("persona_snapshot", {}).get("state", {}), **job.get("state_patch", {})} if job.get("mode") == "persona" else {}, "requirements": job.get("requirements", {}), "request_basis": job.get("request_basis", {}), "request_summary": job.get("request_summary", {}), "mode": job.get("mode"), "error": job.get("error", ""), "trace": job.get("trace", [])}, "photo:" + job["id"])

    def _fail_job(self, job_id, exc, stage=""):
        current = self.storage.job(job_id) or {}
        if current.get("delivery_blocked"):
            return current
        if current.get("status") in {"failed", "cancelled", "uncertain", "delivery_failed"}:
            return current
        error = self._error(exc)
        stage = stage or current.get("preparation_stage") or "request"
        self.storage.update_job(job_id, {"status": "failed", "phase": "failed", "error": error, "failure_stage": stage})
        failed = self._trace_job(job_id, stage, "failed", error)
        self._photo_note(failed)
        self.storage.append_history({"ok": False, "job_id": job_id, "caption": failed.get("caption") or failed.get("raw", ""), "provider": failed.get("provider", ""), "mode": failed.get("mode", ""), "error": error, "failure_stage": stage, **{k: copy.deepcopy(failed[k]) for k in ("prompt", "negative_prompt", "state_patch", "requirements", "reference_source", "request_summary", "trace") if k in failed}})
        return failed

    async def _notify_photo_failure(self, job, *, event=None):
        """Notify independently of the original event, retaining uncertain sends."""
        current = self.storage.job(job["id"]) or job
        if self._closing or current.get("cancel_requested") or current.get("status") == "cancelled" or current.get("notification_state") in {"sending", "accepted", "failed", "uncertain"}:
            return current
        if current.get("status") not in {"failed", "delivery_failed", "uncertain"}:
            return current
        context_send = getattr(self.context, "send_message", None)
        event_send = getattr(event, "send", None)
        umo = str(current.get("umo") or "")
        webchat = self._is_webchat(umo, event=event, job=current)
        channel = "context" if umo and callable(context_send) else "event" if not webchat and callable(event_send) else "none"
        message = str(current.get("error") or "照片任务未完成")
        if current.get("status") == "failed":
            message = "这次照片没能完成：" + message
        # Persist before awaiting: duplicate failure handlers must not send twice.
        self.storage.update_job(current["id"], {"notification_state": "sending", "notification_channel": channel, "notification_error": ""})
        state, error = "failed", "没有可用的失败通知发送通道"
        try:
            if channel != "none":
                chain = MessageChain().message(message)
                accepted = await context_send(umo, chain) if channel == "context" else await event_send(chain)
                state, error = ("failed", "平台未接受失败通知") if accepted is False else ("accepted", "")
                if webchat and accepted is not False:
                    state, error = "uncertain", "WebChat 会话发送调用已完成，但未提供通知投递或历史保存确认"
        except asyncio.CancelledError:
            self.storage.update_job(current["id"], {"notification_state": "uncertain", "notification_error": "通知发送被停止，结果不确定"})
            self._photo_note(self._trace_job(current["id"], "notification", "uncertain", "通知发送被停止，结果不确定"))
            raise
        except Exception as exc:
            # The platform may have accepted the message before a transport error.
            # Retain ambiguity instead of sending a duplicate via another channel.
            state, error = "uncertain", self._error(exc)
        updated = self.storage.update_job(current["id"], {"notification_state": state, "notification_error": error})
        safe_error = updated.get("notification_error", "")
        updated = self._trace_job(current["id"], "notification", state, safe_error or "失败通知发送调用已完成，终端送达未知")
        self._photo_note(updated)
        if state != "accepted":
            logger.warning("[随想画卷] 照片任务 %s 失败通知%s（%s）：%s", current["id"], "结果不确定" if state == "uncertain" else "未发送", channel, safe_error)
        return updated

    def _prepare_request(self, env, text, *, explicit=False):
        pending = env["session"].get("pending") or {}
        if pending and not valid_pending(pending, env):
            session = self.storage.read_session(env["key"])
            if not session or session.get("persona_id") != env["persona"]["id"]:
                return
            session["pending"] = {}
            env["session"] = self.storage.save_session(env["key"], session)
        elif pending and not is_confirmation(text) and (explicit or photo_request(text, pending) or state_request(text, pending)):
            env["changed_request"] = True
            env["previous_conditions"] = pending

    def _save_conditions(self, env, text, reply, required=None, kind="photo", *, replace=False):
        session = self.storage.read_session(env["key"])
        if not session or session.get("persona_id") != env["persona"]["id"]:
            raise ValueError("会话角色已改变，请重新读取当前视觉档案")
        supplied = requirements(required)
        if env.get("changed_request") and not replace:
            required = {**requirements((env.get("previous_conditions") or {}).get("requirements")), **infer_requirements(text), **supplied}
        required = {**requirements(required), "notes": supplied.get("notes", str(reply)[:1000])}
        session["pending"] = pending_request(env, text, reply, required, kind=kind, ttl=int(self.storage.settings["dialogue"].get("confirmation_ttl_sec", 1800)))
        env["session"] = self.storage.save_session(env["key"], session)
        return session["pending"]

    async def _cancel_job(self, job_id):
        job = self.storage.job(job_id)
        if not job:
            raise ValueError("任务不存在")
        if job["status"] not in {"queued", "deciding", "generating", "succeeded"}:
            raise ValueError("任务已结束或已经开始发送，无法撤回；发送结果不确定时请检查聊天记录")
        job = self.storage.update_job(job_id, {"status": "cancelled", "cancel_requested": True, "error": "用户已撤回本次拍摄，不再发送照片"})
        job = self._trace_job(job_id, "cancel", "cancelled", "用户撤回；停止等待与发送，上游已受理任务可能仍计费")
        self._photo_note(job)
        task = self._job_tasks.get(job_id)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        return {"job_id": job_id, "status": "cancelled", "upstream_may_continue": True}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def cancel_photo(self, event: AstrMessageEvent):
        if _extra(event, "handled") or not _addressed(event) or not cancel_request(_text(event)):
            return
        env = await self._env(event)
        current = self.storage.read_session(env["key"])
        if not current or current.get("persona_id") != env["persona"]["id"]:
            return
        env["session"] = current
        active_jobs = [self.storage.job(j) for j in self._job_tasks]
        if not env["session"].get("pending") and not any(j and j.get("session_key") == env["key"] and j.get("status") in {"queued", "deciding", "generating", "succeeded"} for j in active_jobs):
            return
        _extra(event, "handled", True, set_value=True)
        _extra(event, "request_cancelled", True, set_value=True)
        event.should_call_llm(False)
        cancelled = 0
        for job_id in list(self._job_tasks):
            job = self.storage.job(job_id)
            if job and job.get("session_key") == env["key"] and job.get("status") in {"queued", "deciding", "generating", "succeeded"}:
                await self._cancel_job(job_id)
                cancelled += 1
        session = self.storage.read_session(env["key"])
        if not session or session.get("persona_id") != env["persona"]["id"]:
            return
        session["pending"] = {}
        self.storage.save_session(env["key"], session)
        yield event.plain_result("已撤回拍摄，停止发送照片。已受理的接口任务可能仍计费。" if cancelled else "好，取消待确认的拍摄；当前没有可撤回的生成任务。")

    async def initialize(self):
        if self.config.get("enable_webui", True):
            self.page_api.register_routes()
        if self.config.get("enable_legacy_webui", False):
            token = self.storage.webui_token(str(self.config.get("webui_token") or ""))
            self.webui = WebUI(self, host=str(self.config.get("webui_host", "127.0.0.1")), port=int(self.config.get("webui_port", 3018)), token=token)
            self.webui.start()
        # Always run the lightweight scheduler, so console toggles take effect.
        self.active = ActiveScheduler(self)
        self._spawn(self.active.run(), "persona-canvas-scheduler")
        self._spawn(self._asset_maintenance(), "persona-canvas-assets")
        for job in self.storage.recent_jobs(10000):
            if job.get("status") in {"queued", "deciding", "generating"}:
                self.storage.update_job(job["id"], {"status": "failed", "error": "任务被重启中断，可在任务历史中重试"})
                self._photo_note(self.storage.job(job["id"]))
            elif job.get("status") == "sending":
                self.storage.update_job(job["id"], {"status": "uncertain", "error": "重启前正在发送，结果不确定，请先检查聊天记录"})
                self._photo_note(self.storage.job(job["id"]))
        for delivery in self.storage.export().get("deliveries", []):
            if delivery.get("kind") == "reply_image":
                if delivery.get("attached"):
                    self.storage.update_delivery(delivery["key"], {"status": "uncertain", "error": "重启前图片已附加回复链，平台发送结果未知，不自动重发"})
                elif delivery.get("status") in {"claimed", "decided", "generating"}:
                    self.storage.update_delivery(delivery["key"], {"status": "skipped", "error": "重启前配图尚未附加，不自动补图"})
            elif delivery.get("status") in {"claimed", "decided", "generating"}:
                interrupted = self.storage.job(delivery.get("job_id", "")) or {}
                if interrupted.get("recovery", {}).get("submitted"):
                    self.storage.update_delivery(delivery["key"], {"status": "skipped", "retryable": False, "skip_reason": "upstream_pending", "error": "工作流可能已提交，须先检查原任务结果，不自动重新生图"})
                else:
                    self.storage.update_delivery(delivery["key"], {"status": "failed", "error": "重启中断，尚未发送，将有限重试", "retry_at": time.time() + 300})
            elif delivery.get("status") == "sending":
                self.storage.update_delivery(delivery["key"], {"status": "uncertain", "error": "重启前正在发送，请先检查聊天记录"})

    async def _asset_maintenance(self):
        while not self._closing:
            try:
                self.storage.maintenance(int(self.storage.settings["generation"].get("max_history", 100)))
                self.storage.cleanup_assets(grace_sec=3600)
            except Exception as exc:
                logger.warning("随想画卷资源清理失败：%s", self._error(exc))
            await asyncio.sleep(300)

    async def terminate(self):
        self._closing = True
        current = asyncio.current_task()
        tasks = (self._tasks | self._requests) - {current}
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)
        if self.webui:
            await self.webui.close()
        self.storage.close()

    def _track_current(self):
        if self._closing:
            raise RuntimeError("插件正在停止")
        task = asyncio.current_task()
        if task and task not in self._requests:
            self._requests.add(task)
            task.add_done_callback(self._requests.discard)

    def _error(self, exc):
        text = str(self.storage._scrub_known_secrets(str(exc)))
        for config in self.storage.settings.get("providers", {}).values():
            for key in ("api_key", "auth_value"):
                secret = str(config.get(key) or "")
                if secret:
                    text = text.replace(secret, "[已隐藏]")
        return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [已隐藏]", text)[:500]

    def _provider(self, name=None):
        key = str(name or self.storage.settings.get("default_provider") or "default")
        config = self.storage.settings.get("providers", {}).get(key)
        if not isinstance(config, dict):
            raise ProviderError("找不到指定的生图接口，请在模型接口页配置")
        provider = provider_from_config(key, copy.deepcopy(config))
        provider.validate_config()
        return provider

    def _is_webchat(self, umo, *, event=None, job=None):
        """Use the platform type, because a UMO starts with an instance ID."""
        if (job or {}).get("delivery_platform") == "webchat":
            return True
        if event is not None and str(getattr(event, "unified_msg_origin", "") or "") == str(umo):
            name = getattr(event, "get_platform_name", None)
            try:
                if callable(name) and str(name()).lower() == "webchat":
                    return True
            except Exception:
                pass
        lookup = getattr(self.context, "get_platform_inst", None)
        if callable(lookup):
            try:
                platform = lookup(str(umo).split(":", 1)[0])
                meta = platform.meta() if platform is not None else None
                name = getattr(meta, "name", None)
                if isinstance(name, str) and name:
                    return name.lower() == "webchat"
            except Exception:
                pass
        # Compatibility for older contexts without platform-instance discovery.
        return str(umo).split(":", 1)[0].lower() == "webchat"

    @asynccontextmanager
    async def _generation_slot(self, source="chat"):
        async with self._slots:
            self._generation_ticket += 1
            waiter = (self._generation_ticket, source in {"active", "morning", "reply_image"}, time.monotonic())
            self._generation_waiters.append(waiter)
            def ready():
                if self._closing:
                    return True
                now = time.monotonic()
                first = min(self._generation_waiters, key=lambda item: (bool(item[1] and now - item[2] < 30), item[0]))
                return first == waiter and self._running < int(self.storage.settings["generation"].get("max_concurrency", 2))
            try:
                await self._slots.wait_for(ready)
                if self._closing:
                    raise RuntimeError("插件正在停止")
                self._running += 1
            finally:
                self._generation_waiters.remove(waiter)
                self._slots.notify_all()
        try:
            yield
        finally:
            async with self._slots:
                self._running -= 1
                self._slots.notify_all()

    async def _delivery_guard(self, *, env=None, job=None, check_identity=True):
        if not check_identity:
            return local_delivery_guard(self, env=env, job=job)
        return await delivery_guard(self, env=env, job=job)

    def _lock(self, key):
        return self._session_locks.setdefault(key, asyncio.Lock())

    async def _env(self, event):
        cached = _extra(event, "environment")
        return cached or await self.dialogue.environment(event.unified_msg_origin)

    def _eligible(self, text, env):
        pending = {**(env["session"].get("pending") or {}), "last_image": env["session"].get("last_image")}
        return photo_request(text, pending)

    def _native_request(self, event, env, kind, request_summary, confirmed_request_id):
        # Re-read conditions so a cached event cannot approve stale/replaced ones.
        env.pop("approved_requirements", None)
        current = self.storage.read_session(env["key"])
        if not current or current.get("persona_id") != env["persona"]["id"]:
            return "会话角色已改变，请重新读取视觉档案并由当前角色判断", "conditions"
        env["session"] = current
        env["persona"]["state"] = copy.deepcopy(current["state"])
        self._prepare_request(env, _text(event))
        if not self.storage.settings["integration"].get("enabled", True):
            return "聊天接入已停用", "request"
        if not _addressed(event):
            return "群聊没有明确唤醒机器人", "request"
        if _extra(event, "request_cancelled"):
            return "本轮已撤回拍摄", "request"
        if _extra(event, "conditions_recorded"):
            return "本轮正在询问条件，请等下一条用户确认", "conditions"
        confirmation = _extra(event, "confirmed_request", {}) or {}
        summary = str(request_summary or confirmation.get("summary") or "").strip()[:1500]
        request_id = str(confirmed_request_id or confirmation.get("request_id") or "").strip()
        pending = env["session"].get("pending") or {}
        if summary or request_id:
            reason = semantic_request_error(_text(event))
            if reason:
                return reason, "request"
            if not summary:
                return "请在 request_summary 说明本轮用户意图与上下文依据", "request"
            if request_id and (not valid_pending(pending, env) or pending.get("request_id") != request_id):
                return "待确认请求已失效或编号不匹配，请读取当前条件，不能沿用旧确认", "conditions"
            if pending:
                if pending.get("request_kind") != kind:
                    return "待确认条件的类型不匹配，换装确认不能授权拍照", "conditions"
                if not request_id:
                    return "已有待确认条件。确认原条件时填写当前 confirmed_request_id；修改要求时先调用 persona_canvas_conditions", "conditions"
                env["approved_requirements"] = requirements(pending.get("requirements"))
            # The model explicitly judged intent. A positive keyword match is
            # unnecessary; revisions use the structured control/conditions tool.
            env.pop("changed_request", None)
            env.pop("previous_conditions", None)
            env["request_basis"] = {"source": "model_tool", "summary": summary, "confirmed_request_id": request_id}
            return "", ""
        eligible = self._eligible(_text(event), env) if kind == "photo" else state_request(_text(event), pending)
        if not eligible:
            return "没有明确的拍摄或状态请求；若上下文已明确，请用 request_summary 提交语义判断，无需让用户重复关键词", "request"
        if env.get("changed_request"):
            return "要求改变，请先重新确认条件", "conditions"
        if pending and valid_pending(pending, env) and pending.get("request_kind") == kind and is_confirmation(_text(event)):
            env["approved_requirements"] = requirements(pending.get("requirements"))
        env["request_basis"] = {"source": "keyword", "summary": _text(event)[:1500]}
        return "", ""

    def _claim_photo(self, event):
        if _extra(event, "photo_attempted"):
            return False
        _extra(event, "photo_attempted", True, set_value=True)
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if message_id:
            key = f"chat:{event.unified_msg_origin}:{message_id}"
            if not self.storage.reserve_delivery(key):
                return False
            self.storage.update_delivery(key, {"kind": "chat", "umo": event.unified_msg_origin, "status": "decided"})
            _extra(event, "delivery_key", key, set_value=True)
        return True

    async def _capture_reference(self, event):
        # Prefer platform image conversion API rather than arbitrary remote URL IO.
        from astrbot.api.message_components import Image
        for component in event.get_messages():
            if isinstance(component, Image):
                method = getattr(component, "convert_to_base64", None)
                if method:
                    encoded = await maybe_await(method())
                    if encoded:
                        data = base64.b64decode(str(encoded).split(",", 1)[-1], validate=True)
                        result = await self.web_upload_reference(data, "chat-reference")
                        asset = result["asset"]
                        return asset, self.storage.lease_asset(asset)
        return "", ""

    @filter.on_llm_request()
    async def inject_visual_state(self, event: AstrMessageEvent, req):
        # A request may pass this hook again. Replace only our own transient
        # block, preserving the system prompt and other plugins' content.
        previous = getattr(req, "_persona_canvas_visual_part", None)
        parts = getattr(req, "extra_user_content_parts", None)
        if previous is not None:
            if isinstance(parts, list):
                parts[:] = [part for part in parts if part is not previous]
            req._persona_canvas_visual_part = None
        self.reply_images.observe(event)
        if not self.storage.settings["integration"].get("enabled", True):
            return
        env = await self.dialogue.environment(event.unified_msg_origin, conversation=getattr(req, "conversation", None))
        self._prepare_request(env, _text(event))
        if self.storage.settings["integration"].get("mode") != "native_tools" and env["session"].get("pending") and not self._eligible(_text(event), env) and not state_request(_text(event), env["session"].get("pending")):
            env["session"]["pending"] = {}
            env["session"] = self.storage.save_session(env["key"], env["session"])
        _extra(event, "environment", env, set_value=True)
        visual_context = self.dialogue.visual_prompt(env)
        if self.storage.settings["integration"].get("mode") == "native_tools" and _addressed(event):
            visual_context += "\n原生工具支持上下文语义判断：你已理解用户要图片/更新外观时，在 request_summary 简述本轮意图与上下文依据，直接调用，无需用户重复关键词。已有条件时用 persona_canvas_control(action=confirm, request_id=当前编号) 记录语义确认，再调用拍照/状态工具；也可在执行工具直接填写 confirmed_request_id。重复约定原衣服不算修改；用户确实修改条件时 control(revise) 记录更新并等待下一轮确认。撤回用 control(cancel)，生成失败需用户明确要求后 control(retry)；已保存照片发送失败用 control(resend, job_id=原任务编号) 补发原图，不重新生成。发送结果不确定时用户先检查聊天记录并明确要求补发，再设 confirm_uncertain=true。修改条件可用 remove_fields 移除约束，仍需重新询问。同意的文字本身不会生图。persona 自拍默认只用角色参考图；需要参考用户附件时显式 use_message_image=true，edit 模式自动读取附件。"
        if self.storage.settings["integration"].get("mode") == "native_tools" and _addressed(event) and self._eligible(_text(event), env):
            visual_context += "\n本轮用户消息已通过明确图片请求检查。这只表示请求有效，不代表你必须同意；你仍可拒绝或先提条件。若你愿意实际提供照片，必须调用 persona_canvas_photo；尚在询问或要求有变化时先记录条件并等待确认。不要只用文字假装已经发图。"
        from astrbot.core.agent.message import TextPart
        part = TextPart(text=visual_context).mark_as_temp()
        if getattr(req, "extra_user_content_parts", None) is None:
            req.extra_user_content_parts = []
        req.extra_user_content_parts.append(part)
        req._persona_canvas_visual_part = part

    @filter.on_llm_response()
    async def remember_conditions(self, event: AstrMessageEvent, response):
        if not self.storage.settings["integration"].get("enabled", True) or _extra(event, "photo_attempted") or _extra(event, "conditions_recorded") or _extra(event, "native_tool_seen"):
            return
        # A tool-call response is not the final reply: AstrBot executes it next.
        # Stream chunks likewise cannot establish that no tool will be called.
        if getattr(response, "tools_call_name", None) or getattr(response, "is_chunk", False):
            return
        env = await self._env(event)
        if await self._delivery_guard(env=env):
            return
        text = str(getattr(response, "completion_text", "") or "")
        session = self.storage.read_session(env["key"])
        if not session or session.get("persona_id") != env["persona"]["id"]:
            return
        pending = session.get("pending") or {}
        if self._eligible(_text(event), env) or state_request(_text(event), pending):
            status = "refuse" if re.search(r"不想拍|不愿意拍|不拍了|不能拍|拒绝", text) else "ask" if re.search(r"可以吗|行吗|好吗|好不好|要不要|愿意吗", text) else "chat"
            trace = [{"stage": "request", "status": "ok"}, {"stage": "role", "status": status, "detail": "标签依据回复文本归纳，不代表已执行拍摄"}]
            if status == "chat" and self._eligible(_text(event), env):
                status = "no_tool"
                trace.append({"stage": "tool", "status": status, "detail": "本轮有明确图片请求，但未调用拍照工具，未创建生成任务。请检查 AstrBot 工具开关、人格工具限制与模型工具调用能力；仅凭文字回复无法确认具体原因。"})
            self.storage.record_action({"kind": "decision", "source": "native", "session_key": env["key"], "persona_id": env["persona"]["id"], "umo": env["umo"], "request": _text(event), "reply": text[:2000], "status": status, "trace": trace})
        native = self.storage.settings["integration"].get("mode") == "native_tools"
        if not native and pending and not self._eligible(_text(event), env) and not state_request(_text(event), pending):
            session["pending"] = {}
            session = self.storage.save_session(env["key"], session)
            pending = {}
        if cancel_request(_text(event)):
            if pending:
                session["pending"] = {}
                self.storage.save_session(env["key"], session)
        elif not pending and (self._eligible(_text(event), env) or state_request(_text(event), pending)) and re.search(r"可以吗|行吗|好吗|好不好|要不要|愿意吗", text):
            self._save_conditions(env, _text(event), text, infer_requirements(text), "photo" if self._eligible(_text(event), env) else "state")
        elif not pending and re.search(r"(?:给你|发你|给你看|要不要)[^。！？\n]{0,20}(?:照片|自拍|拍一张)", text) and re.search(r"可以吗|好吗|要不要|想看吗|好不好", text):
            self._save_conditions(env, "角色主动提议拍照，等待用户确认", text, infer_requirements(text))

    @filter.on_decorating_result()
    async def decorate_reply_image(self, event: AstrMessageEvent):
        await self.reply_images.decorate(event)

    @filter.llm_tool(name="persona_canvas_conditions")
    async def tool_conditions(self, event: AstrMessageEvent, reply: str, request_kind: str = "photo", outfit: str = "", camera: str = "", pose: str = "", expression: str = "", scene: str = "", avoid: str = "", request_summary: str = ""):
        """你愿意提拍摄或换装条件时先记录条件、询问用户确认，本轮不生图。

        Args:
            reply (string): 对用户说的条件与确认问题。
            request_kind (string): photo 拍照条件，state 仅换装或姿势条件。
            outfit (string): 必须穿着的衣服，未限定留空。
            camera (string): 镜头距离或构图条件，未限定留空。
            pose (string): 姿势条件，未限定留空。
            expression (string): 表情条件，未限定留空。
            scene (string): 场景条件，未限定留空。
            avoid (string): 明确不能拍的内容或角度，未限定留空。
            request_summary (string): 从本轮消息及上下文判断出的用户图片/状态意图，不要求固定关键词；修改条件时说明修改点。
        """
        env = await self._env(event)
        _extra(event, "native_tool_seen", True, set_value=True)
        guard_reason = await self._delivery_guard(env=env)
        if guard_reason:
            return json.dumps({"ok": False, "reason": guard_reason}, ensure_ascii=False)
        self._prepare_request(env, _text(event))
        semantic = bool(str(request_summary or "").strip())
        if not self.storage.settings["integration"].get("enabled", True) or not _addressed(event) or request_kind not in {"photo", "state"} or (semantic_request_error(_text(event)) if semantic else not (self._eligible(_text(event), env) or state_request(_text(event), env["session"].get("pending")))):
            return json.dumps({"ok": False, "reason": "当前没有明确的拍摄或状态请求"}, ensure_ascii=False)
        if _extra(event, "photo_attempted") or not reply.strip():
            return json.dumps({"ok": False, "reason": "本轮已安排拍摄或缺少确认问题"}, ensure_ascii=False)
        if valid_pending(env["session"].get("pending"), env):
            env["changed_request"] = True
            env["previous_conditions"] = env["session"]["pending"]
            previous_job = self.storage.job(env["previous_conditions"].get("execution_job_id", "")) or {}
            if previous_job.get("session_key") == env["key"] and previous_job.get("persona_id") == env["persona"]["id"] and previous_job.get("status") in {"queued", "deciding", "generating", "succeeded"}:
                # Replacing an agreement also withdraws its unfinished photo.
                # Keeping the newer state alone would still send the old image.
                await self._cancel_job(previous_job["id"])
        pending = self._save_conditions(env, _text(event), reply, {"outfit": outfit, "camera": camera, "pose": pose, "expression": expression, "scene": scene, "avoid": avoid}, request_kind)
        _extra(event, "conditions_recorded", True, set_value=True)
        self.storage.record_action({"session_key": env["key"], "persona_id": env["persona"]["id"], "umo": env["umo"], "kind": "decision", "status": "ask", "request": _text(event), "request_basis": {"source": "model_tool" if semantic else "keyword", "summary": str(request_summary or _text(event))[:1500]}, "reply": reply, "requirements": pending["requirements"]})
        return json.dumps({"ok": True, "request_id": pending["request_id"], "instruction": "现在向用户询问这些条件。本轮不得拍照或更新状态，等下一条明确确认。"}, ensure_ascii=False)

    @filter.llm_tool(name="persona_canvas_control")
    async def tool_control(self, event: AstrMessageEvent, action: str, request_summary: str, request_id: str = "", job_id: str = "", reply: str = "", updates: dict | None = None, remove_fields: list[str] | None = None, confirm_uncertain: bool = False):
        """根据本轮上下文确认、修改、撤回或显式重试图片请求。不会因一句同意自行生成。

        Args:
            action (string): confirm 确认原条件；revise 修改并询问；cancel 撤回；retry 重试生成失败；resend 补发已保存原图，不重新生成。
            request_summary (string): 本轮语义判断依据，确认、修改或重试必须来自当前用户意图。
            request_id (string): 当前 pending_conditions.request_id，confirm/revise 必填。
            job_id (string): 撤回、重试或补发的任务编号，retry/resend 必填。
            reply (string): revise 时对用户说的更新条件与确认问题。
            updates (object): revise 的结构化新条件，支持 outfit/camera/pose/expression/scene/avoid/notes。confirm 可填写重复的原条件，不得改变值。
            remove_fields (array[string]): revise 要移除的条件字段；updates 中空字符串或 null 同样表示移除。修改后需重新询问。
            confirm_uncertain (boolean): 用户已检查聊天记录并明确确认未收到时为 true；用于补发 uncertain 照片，或恢复旧版误记 sent 的原图。没有明确核对时不得设置。
        """
        self._track_current()
        _extra(event, "native_tool_seen", True, set_value=True)
        env = await self._env(event)
        try:
            if not self.storage.settings["integration"].get("enabled", True) or not _addressed(event):
                raise ValueError("聊天接入停用或群聊未唤醒机器人")
            if not str(request_summary or "").strip():
                raise ValueError("请说明本轮用户意图与上下文依据")
            current = self.storage.read_session(env["key"])
            if not current or current.get("persona_id") != env["persona"]["id"]:
                raise ValueError("会话角色已改变，请重新读取当前视觉档案")
            env["session"] = current
            if action != "cancel":
                guard_reason = await self._delivery_guard(env=env)
                if guard_reason:
                    raise ValueError(guard_reason)
            pending = current.get("pending") or {}
            if action in {"confirm", "revise"}:
                if not request_id or not valid_pending(pending, env) or pending.get("request_id") != request_id:
                    raise ValueError("待确认请求已失效或编号不匹配")
                if _extra(event, "conditions_recorded") or _extra(event, "photo_attempted") or _extra(event, "request_cancelled"):
                    raise ValueError("本轮已经询问、执行或撤回，不能再次确认")
                reason = semantic_request_error(_text(event))
                if reason:
                    raise ValueError(reason)
                if updates is not None and not isinstance(updates, dict):
                    raise ValueError("updates 必须是条件对象")
                if remove_fields is not None and (not isinstance(remove_fields, list) or any(not isinstance(k, str) or k not in REQUIREMENT_KEYS for k in remove_fields)):
                    raise ValueError("remove_fields 必须是有效条件字段列表")
                if updates and any(k not in REQUIREMENT_KEYS or (v is not None and not isinstance(v, str)) for k, v in updates.items()):
                    raise ValueError("updates 仅支持有效条件字段与字符串或 null 值")
                removed = set(remove_fields or []) | {k for k, v in (updates or {}).items() if v is None or not v.strip()}
                normalized = requirements(updates)
                original = requirements(pending.get("requirements"))
                if action == "confirm":
                    if any(original.get(k) != v for k, v in normalized.items()) or any(k in original for k in removed):
                        raise ValueError("结构化条件发生变化，请先 revise 并重新询问")
                    _extra(event, "confirmed_request", {"request_id": request_id, "summary": str(request_summary)[:1500]}, set_value=True)
                    return json.dumps({"ok": True, "request_id": request_id, "job_id": pending.get("execution_job_id", ""), "instruction": "原条件已确认。你愿意执行时再调用拍照或状态工具；已有任务时不会重复入队。"}, ensure_ascii=False)
                if not str(reply or "").strip():
                    raise ValueError("修改条件需要向用户提出新的确认问题")
                if pending.get("execution_job_id"):
                    old = self.storage.job(pending["execution_job_id"]) or {}
                    if old.get("status") in {"queued", "deciding", "generating", "succeeded"}:
                        await self._cancel_job(old["id"])
                env["changed_request"], env["previous_conditions"] = True, pending
                revised = {k: v for k, v in {**original, **normalized}.items() if k not in removed}
                # Notes describe the current agreement, never the superseded one.
                revised["notes"] = normalized.get("notes", str(reply))[:1000]
                changed = self._save_conditions(env, _text(event), reply, revised, pending.get("request_kind", "photo"), replace=True)
                _extra(event, "conditions_recorded", True, set_value=True)
                _extra(event, "confirmed_request", {}, set_value=True)
                return json.dumps({"ok": True, "request_id": changed["request_id"], "instruction": "向用户询问更新条件，本轮不得执行。"}, ensure_ascii=False)
            if action == "cancel":
                if request_id and pending.get("request_id") != request_id:
                    raise ValueError("待撤回的条件编号已改变")
                target = self.storage.job(job_id) if job_id else None
                if job_id and (not target or target.get("session_key") != env["key"] or target.get("persona_id") != env["persona"]["id"]):
                    raise ValueError("任务不存在或不属于当前会话角色")
                ids = [job_id] if job_id else [j for j in self._job_tasks if (self.storage.job(j) or {}).get("session_key") == env["key"]]
                cancelled = []
                for identifier in ids:
                    job = self.storage.job(identifier) or {}
                    if job.get("status") in {"queued", "deciding", "generating", "succeeded"}:
                        await self._cancel_job(identifier)
                        cancelled.append(identifier)
                    elif job_id and job.get("status") in {"sent", "sending", "uncertain"}:
                        raise ValueError("任务已发送或发送结果不确定，请检查聊天记录，无法撤回已发照片")
                    elif job.get("status") in {"failed", "cancelled", "delivery_failed"}:
                        self.storage.update_job(identifier, {"cancel_requested": True, "error": "用户已撤回本次请求，不能重试旧授权"})
                if not job_id or pending.get("execution_job_id") == job_id:
                    current["pending"] = {}
                    env["session"] = self.storage.save_session(env["key"], current)
                _extra(event, "request_cancelled", True, set_value=True)
                return json.dumps({"ok": True, "cancelled_jobs": cancelled, "instruction": "本次请求已撤回，不能继续调用拍照工具。"}, ensure_ascii=False)
            if action == "retry":
                reason = semantic_request_error(_text(event))
                if reason or not job_id or _extra(event, "photo_attempted") or _extra(event, "conditions_recorded"):
                    raise ValueError(reason or "请填写失败任务编号；本轮已执行或询问时不能重试")
                old = self.storage.job(job_id) or {}
                if old.get("session_key") != env["key"] or old.get("persona_id") != env["persona"]["id"]:
                    raise ValueError("失败任务不属于当前会话角色")
                if not self._claim_photo(event):
                    raise ValueError("本轮已安排拍摄，请勿重复重试")
                return json.dumps({"ok": True, **await self._retry_job({"id": job_id}, event=event)}, ensure_ascii=False)
            if action == "resend":
                if cancel_request(_text(event)) or _extra(event, "request_cancelled") or _extra(event, "conditions_recorded"):
                    raise ValueError("本轮已撤回或修改条件，不能补发")
                old = self.storage.job(job_id) or {}
                if not job_id or old.get("session_key") != env["key"] or old.get("persona_id") != env["persona"]["id"]:
                    raise ValueError("照片任务不属于当前会话角色")
                if not self._claim_photo(event):
                    raise ValueError("本轮已执行，请勿重复补发")
                return json.dumps(await self._resend_job({"id": job_id, "confirm_uncertain": confirm_uncertain}, event=event), ensure_ascii=False)
            raise ValueError("action 仅支持 confirm/revise/cancel/retry/resend")
        except Exception as exc:
            return json.dumps({"ok": False, "reason": self._error(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="persona_canvas_state")
    async def tool_state(self, event: AstrMessageEvent, outfit: str = "", pose: str = "", expression: str = "", scene: str = "", request_summary: str = "", confirmed_request_id: str = ""):
        """你愿意接受用户明确提出的换装/姿势要求时，更新会话视觉状态，不会拍照。拒绝或提条件时不要调用。

        Args:
            outfit (string): 新服装，未改变留空。
            pose (string): 新姿势，未改变留空。
            expression (string): 新表情，未改变留空。
            scene (string): 新场景，未改变留空。
            request_summary (string): 根据本轮消息和上下文判断的换装/状态请求或条件确认依据，填写后不要求固定关键词。
            confirmed_request_id (string): 用户已确认原条件时填写 pending_conditions.request_id；没有条件时留空，修改要求先重新提条件。
        """
        self._track_current()
        _extra(event, "native_tool_seen", True, set_value=True)
        env = await self._env(event)
        reason, _stage = self._native_request(event, env, "state", request_summary, confirmed_request_id)
        if reason:
            return json.dumps({"ok": False, "reason": reason}, ensure_ascii=False)
        guard_reason = await self._delivery_guard(env=env)
        if guard_reason:
            return json.dumps({"ok": False, "reason": guard_reason}, ensure_ascii=False)
        patch = state_patch({k: v for k, v in {"outfit": outfit, "pose": pose, "expression": expression, "scene": scene}.items() if v})
        if is_confirmation(_text(event)):
            patch.update(state_patch((env["session"].get("pending") or {}).get("requirements")))
        patch.update(state_patch(env.get("approved_requirements")))
        async with self._lock(env["key"]):
            guard_reason = local_delivery_guard(self, env=env)
            if guard_reason:
                return json.dumps({"ok": False, "reason": guard_reason}, ensure_ascii=False)
            session = self.storage.read_session(env["key"])
            if not session or session.get("persona_id") != env["persona"]["id"] or session.get("revision") != env["session"].get("revision"):
                return json.dumps({"ok": False, "reason": "会话状态或条件已改变，请重新判断"}, ensure_ascii=False)
            apply_state(session, patch, self.storage.settings)
            session["pending"] = {}
            session = self.storage.save_session(env["key"], session)
            env["session"] = session
            env["persona"]["state"] = session["state"]
        return json.dumps({"ok": True, "state": session["state"], "photo_generated": False}, ensure_ascii=False)

    @filter.llm_tool(name="persona_canvas_photo")
    async def tool_photo(self, event: AstrMessageEvent, prompt: str, mode: str = "persona", caption: str = "", outfit: str = "", pose: str = "", expression: str = "", scene: str = "", use_last_image: bool = False, request_summary: str = "", confirmed_request_id: str = "", use_message_image: bool = False):
        """根据上下文判断用户要照片/绘图且你愿意时，直接实际生图并发图，无需用户重复关键词。填写 request_summary；已有条件时还需 confirmed_request_id。普通聊天、否定、引用、拒绝、提条件时不得调用。一次请求一次图片。

        Args:
            prompt (string): 你同意的拍摄描述，体现镜头、动作与情绪，保持固定长相。
            mode (string): persona 人设自拍，scene 普通场景绘图，edit 真实参考图编辑。
            caption (string): 图片附带的简短角色台词。
            outfit (string): 本次持续换装，未改变留空。
            pose (string): 新姿势，未改变留空。
            expression (string): 新表情，未改变留空。
            scene (string): 新场景，未改变留空。
            use_last_image (boolean): 编辑上一张图片时为 true。
            use_message_image (boolean): 明确需要用户附件作为参考时为 true；persona 自拍默认 false，只用角色参考图。edit 自动读取附件。
            request_summary (string): 简述本轮用户要图片或已确认原条件的语义及上下文依据，填写后不要求原话命中拍照关键词。
            confirmed_request_id (string): 已有拍摄条件且用户未修改要求地确认时，填写 pending_conditions.request_id；没有条件留空，修改要求先调用 persona_canvas_conditions。
        """
        self._track_current()
        _extra(event, "native_tool_seen", True, set_value=True)
        env = await self._env(event)
        def rejected(reason, stage):
            self.storage.record_action({"kind": "decision", "source": "native_tool", "session_key": env["key"], "persona_id": env["persona"]["id"], "umo": env["umo"], "request": _text(event), "status": "blocked", "error": reason, "trace": [{"stage": stage, "status": "blocked", "detail": reason}]})
            return json.dumps({"ok": False, "reason": reason}, ensure_ascii=False)
        reason, stage = self._native_request(event, env, "photo", request_summary, confirmed_request_id)
        if reason:
            return rejected(reason, stage)
        pending = env["session"].get("pending") or {}
        def existing_result(identifier):
            existing = self.storage.job(identifier) or {}
            return json.dumps({"ok": existing.get("status") not in {"failed", "cancelled", None}, "job_id": identifier, "status": existing.get("status", "missing"), "reused": True, "instruction": "该请求已有任务，不再重复生成。失败后需用户明确要求重试，并调用 persona_canvas_control(action=retry, job_id=该编号)。"}, ensure_ascii=False)
        if pending.get("execution_job_id"):
            return existing_result(pending["execution_job_id"])
        if not self._claim_photo(event):
            return json.dumps({"ok": False, "reason": "本轮已经执行过拍摄，请不要重复调用"}, ensure_ascii=False)
        patch = {k: v for k, v in {"outfit": outfit, "pose": pose, "expression": expression, "scene": scene}.items() if v}
        lease = ""
        job = None
        try:
            reference = env["session"].get("last_image", "") if use_last_image else ""
            reference_source = "last_image" if reference else "persona" if mode == "persona" else "none"
            if mode not in {"persona", "scene", "edit"} or not prompt.strip():
                raise ValueError("拍摄模式或描述无效")
            if (mode == "edit" and not use_last_image) or use_message_image:
                reference, lease = await self._capture_reference(event)
                if not reference:
                    raise ValueError("当前消息没有可读取的图片附件；编辑上一张图可设置 use_last_image=true")
                reference_source = "message_attachment"
            # Attachment conversion can await; conditions may have changed meanwhile.
            reason, stage = self._native_request(event, env, "photo", request_summary, confirmed_request_id)
            if reason:
                return rejected(reason, stage)
            pending = env["session"].get("pending") or {}
            reference_assets = [reference] if reference else list(dict.fromkeys(([env["persona"].get("reference_asset")] if env["persona"].get("reference_asset") else []) + env["persona"].get("reference_assets", []))) if mode == "persona" and env["persona"].get("reference_enabled") else []
            prepared = self._prepare_generation(env, prompt, mode, patch, caption, _text(event), reference_asset=reference, source="chat", reference_source=reference_source)
            selected_provider = prepared["provider"].name
            job = self.storage.create_job({"status": "queued", "phase": "local_queued", "preflight_checked": True, "provider": selected_provider, "request_summary": self._request_summary(prepared["provider"], prepared["positive"], prepared["negative"], prepared["options"], len(prepared["reference_names"]), reference_source), "source": "chat", "caption": caption, "raw": _text(event), "mode": mode, "reference_asset": reference, "reference_assets": reference_assets, "reference_source": reference_source, "request_basis": copy.deepcopy(env.get("request_basis", {})), "delivery_key": _extra(event, "delivery_key", ""), "umo": env["umo"], "session_key": env["key"], "persona_id": env["persona"]["id"]})
            if self._is_webchat(env["umo"], event=event):
                self.storage.update_job(job["id"], {"delivery_platform": "webchat"})
            if pending and env.get("approved_requirements") is not None:
                claim = self.storage.claim_photo_request(env["key"], env["persona"]["id"], pending["request_id"], job["id"])
                if not claim["claimed"]:
                    self.storage.update_job(job["id"], {"status": "cancelled", "error": "相同条件已有生成任务", "duplicate_of": claim["job_id"]})
                    return existing_result(claim["job_id"])
                env["session"] = claim["session"]
                self.storage.update_job(job["id"], {"confirmed_request_id": pending["request_id"]})
            self._photo_note(job)
            async def run():
                try:
                    generated = await self._generate_job(env, prompt, mode, patch, caption, _text(event), user_key=f"{event.get_platform_id()}:{event.get_sender_id()}", is_admin=_admin(event), reference_asset=reference, provider_name=selected_provider, source="chat", job_id=job["id"], reference_source=reference_source)
                    await self._send_job(generated, umo=generated["umo"])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    current = self.storage.job(job["id"]) or {}
                    self._fail_job(job["id"], exc)
                    self._photo_note(self.storage.job(job["id"]))
                    if current.get("delivery_key") and current.get("status") not in {"uncertain", "delivery_failed"}:
                        self.storage.update_delivery(current["delivery_key"], {"status": "failed", "error": self._error(exc)})
                    await self._notify_photo_failure(self.storage.job(job["id"]), event=event)
            self._spawn(run(), "persona-canvas-chat-photo", job["id"])
            return json.dumps({"ok": True, "job_id": job["id"], "status": "queued", "phase": "local_queued", "provider": selected_provider, "preflight_checked": True, "submitted": False, "upstream_accepted": False, "image_sent": False, "description": prompt, "instruction": "本地配置与请求预检已通过，插件已创建本地任务，等待后台提交绘图接口；上游尚未确认受理。可以说明请求已交给插件；不能声称正在生成、上游已排队或照片已完成。本轮不要重复调用，后台失败会尝试通知。"}, ensure_ascii=False)
        except Exception as exc:
            if job and (self.storage.job(job["id"]) or {}).get("status") == "queued":
                self.storage.update_job(job["id"], {"status": "failed", "error": self._error(exc)})
            reason = self._error(exc)
            self.storage.record_action({"kind": "decision", "source": "native_tool", "session_key": env["key"], "persona_id": env["persona"]["id"], "umo": env["umo"], "request": _text(event), "status": "blocked", "error": reason, "trace": [{"stage": "preflight", "status": "failed", "detail": reason}]})
            return json.dumps({"ok": False, "phase": "preflight", "submitted": False, "upstream_accepted": False, "reason": reason, "instruction": "本地预检失败，未提交绘图接口。如实说明原因，不能声称正在生成或已拍好；本轮不要重复调用。"}, ensure_ascii=False)
        finally:
            if lease:
                self.storage.release_asset(lease)

    def _timeouts(self, provider, options=None):
        provider_timeout = max(5, min(600, int(getattr(provider, "config", {}).get("timeout", 180))))
        task_timeout = max(5, min(600, int((options or self.storage.settings["generation"]).get("timeout_sec", 180))))
        return provider_timeout, task_timeout, min(provider_timeout, task_timeout)

    def _request_summary(self, provider, positive, negative, options, count, reference_source):
        provider_timeout, task_timeout, effective = self._timeouts(provider, options)
        actual = getattr(provider, "last_request", {}) or {}
        allowed = {"prompt", "negative_prompt", "negative_mode", "negative_prompt_field", "negative_source", "model", "options", "reference_count", "provider_timeout_sec", "notes", "submitted", "workflow_prompt_id", "output_node"}
        summary = {k: copy.deepcopy(v) for k, v in actual.items() if k in allowed}
        if not summary:
            summary = {"submitted": False, "prompt": positive, "negative_prompt": negative if provider.capabilities.negative_prompt else "", "negative_mode": getattr(provider.capabilities, "negative_mode", "disabled"), "model": getattr(provider, "config", {}).get("model", ""), "reference_count": count, "notes": "请求尚未提交，显示编排输入；接口实际参数以提交后摘要为准"}
        summary.update(reference_source=reference_source, provider_timeout_sec=provider_timeout, task_timeout_sec=task_timeout, effective_timeout_sec=effective)
        # Summaries intentionally exclude bodies, headers, credentials and image bytes.
        for name, value in list(summary.items()):
            if isinstance(value, str):
                for config in self.storage.settings.get("providers", {}).values():
                    for key in ("api_key", "auth_value"):
                        secret = str(config.get(key) or "")
                        if secret:
                            value = value.replace(secret, "[已隐藏]")
                summary[name] = value
        return summary

    def _prepare_generation(self, env, prompt, mode="persona", patch=None, caption="", raw="", *, reference_asset="", provider_name="", options=None, source="webui", job_id="", reference_source="", explicit_retry=False):
        """Local-only preparation, shared by admission and background dispatch."""
        if job_id and (self.storage.job(job_id) or {}).get("cancel_requested"):
            raise asyncio.CancelledError()
        if mode not in {"persona", "scene", "edit"} or not str(prompt).strip():
            raise ValueError("请选择有效拍摄模式并填写描述")
        pending = env["session"].get("pending") or {}
        agreed = requirements(env.get("approved_requirements") or (pending.get("requirements") if valid_pending(pending, env) and pending.get("request_kind") == "photo" and is_confirmation(raw) else {}))
        patch = {**state_patch(patch), **state_patch(agreed)}
        persona = copy.deepcopy(env["persona"])
        intent = Intent(mode={"persona": "persona_selfie", "scene": "scene", "edit": "image_edit"}[mode], use_persona=mode == "persona", prompt_delta=prompt if mode == "persona" else "", scene_prompt=prompt if mode != "persona" else "", state_patch=patch, raw=raw, caption=caption)
        positive, negative = prompt_bundle(persona, intent)
        if source in {"active", "morning"} and env.get("proactive_context", {}).get("time"):
            positive += "\nCurrent capture time (scene context only; do not render as text):\n" + json.dumps(env["proactive_context"]["time"], ensure_ascii=False) + "\nUse this same current time for the photograph and caption. Respect indoor lighting and the agreed scene; a custom morning window does not imply sunrise."
        if source in {"active", "morning"} and env.get("schedule_context"):
            positive += "\nCurrent role schedule (scene context only; do not render this as text):\n" + env["schedule_context"] + "\nThe photo's setting, outfit and action should fit the current activity, not a future plan."
        visual_conditions = {k: agreed[k] for k in ("outfit", "camera", "pose", "expression", "scene") if agreed.get(k)}
        if visual_conditions:
            positive += "\nMandatory agreed shooting conditions: " + json.dumps(visual_conditions, ensure_ascii=False)
        if agreed.get("avoid"):
            negative = ", ".join(value for value in (negative, agreed["avoid"]) if value)
        options = {**copy.deepcopy(self.storage.settings["generation"]), **(options or {})}
        if job_id:
            self.storage.update_job(job_id, {"prompt": positive, "negative_prompt": negative, "request_prompt": prompt, "persona_snapshot": persona, "state_patch": patch, "requirements": agreed, "options": options, "provider": str(provider_name or self.storage.settings.get("default_provider", "")), "preparation_stage": "request", "request_summary": {"submitted": False, "prompt": positive, "negative_prompt": "", "notes": "请求尚未提交，接口未完成初始化；此处为编排输入"}})
        self.moderation.check_content(raw + "\n" + positive)
        if job_id:
            self.storage.update_job(job_id, {"preparation_stage": "provider"})
        provider = self._provider(provider_name)
        explicit_retry = explicit_retry or bool(job_id and (self.storage.job(job_id) or {}).get("retry_of"))
        if source in {"active", "morning", "reply_image"} and not explicit_retry and not getattr(provider.capabilities, "automated_generation", True):
            raise ValueError("此生图接口要求人工发起生成，不能用于定时主动照片或对话随机配图；请选择支持自动生成的接口")
        reference_names = env.get("approved_reference_assets") or ([reference_asset] if reference_asset else (list(dict.fromkeys(([persona["reference_asset"]] if persona.get("reference_asset") else []) + persona.get("reference_assets", []))) if persona.get("reference_enabled") and mode == "persona" else []))
        reference_limit = int(getattr(provider.capabilities, "max_reference_images", 1))
        reference_warning = f"接口单次支持 {reference_limit} 张参考图，使用列表中前 {reference_limit} 张" if len(reference_names) > reference_limit else ""
        reference_names = reference_names[:reference_limit]
        reference_name = reference_names[0] if reference_names else ""
        reference_source = reference_source or ("explicit_reference" if reference_asset else "persona" if reference_names and mode == "persona" else "none")
        if job_id:
            self.storage.update_job(job_id, {"preparation_stage": "reference"})
        if mode == "edit" and not reference_name:
            raise ValueError("图片编辑需要上传参考图或选择上一张照片")
        if reference_names and not provider.capabilities.image_to_image:
            raise ValueError("当前接口不支持参考图，请选择支持图像输入的接口")
        leases = []
        try:
            for name in reference_names:
                leases.append(self.storage.lease_asset(name))
            reference_data = [self.storage.asset(name).read_bytes() for name in reference_names]
        finally:
            for token in leases:
                self.storage.release_asset(token)
        reference = reference_data if len(reference_data) > 1 else reference_data[0] if reference_data else None
        if job_id:
            self.storage.update_job(job_id, {"preparation_stage": "provider"})
        preflight = getattr(provider, "preflight", None)
        if callable(preflight):
            preflight(positive, negative, reference=reference, options=options)
        return {"persona": persona, "patch": patch, "agreed": agreed, "positive": positive, "negative": negative, "options": options, "provider": provider, "reference_names": reference_names, "reference_name": reference_name, "reference": reference, "reference_source": reference_source, "reference_warning": reference_warning}

    async def _generate_job(self, env, prompt, mode="persona", patch=None, caption="", raw="", *, user_key="webui", is_admin=False, reference_asset="", provider_name="", options=None, source="webui", job_id="", reference_source=""):
        self._track_current()
        try:
            prepared = self._prepare_generation(env, prompt, mode, patch, caption, raw, reference_asset=reference_asset, provider_name=provider_name, options=options, source=source, job_id=job_id, reference_source=reference_source)
        except Exception as exc:
            if job_id:
                self._fail_job(job_id, exc)
            raise
        persona, patch, agreed = prepared["persona"], prepared["patch"], prepared["agreed"]
        positive, negative, options, provider = prepared["positive"], prepared["negative"], prepared["options"], prepared["provider"]
        reference_names, reference_name, reference = prepared["reference_names"], prepared["reference_name"], prepared["reference"]
        reference_source, reference_warning = prepared["reference_source"], prepared["reference_warning"]
        job_data = {"status": "queued", "source": source, "mode": mode, "umo": env["umo"], "session_key": env["key"], "persona_id": persona["id"], "persona_snapshot": persona, "session_revision": env["session"].get("revision", 0), "prompt": positive, "negative_prompt": negative, "request_prompt": prompt, "raw": raw, "caption": caption, "state_patch": patch, "provider": provider.name, "reference_asset": reference_name, "reference_assets": reference_names, "reference_source": reference_source, "reference_warning": reference_warning, "requirements": agreed, "options": options, "request_summary": self._request_summary(provider, positive, negative, options, len(reference_names), reference_source)}
        job = self.storage.update_job(job_id, job_data) if job_id else self.storage.create_job(job_data)
        job = self.storage.update_job(job["id"], {"identity": {name: env.get(name, "") for name in ("astro_id", "cid")}, "delivery_revision": env["session"].get("revision", 0), "delivery_pending_id": (env["session"].get("pending") or {}).get("request_id", "")})
        if callable(getattr(provider, "recover", None)):
            async def save_progress(record):
                current = self.storage.job(job["id"])
                self.storage.update_job(job["id"], {"recovery": {**(current.get("recovery") or {}), **record}, "request_summary": self._request_summary(provider, positive, negative, options, len(reference_names), reference_source)})
            provider.on_progress = save_progress
        self._bind_job_task(job["id"], asyncio.current_task())
        basis = job.get("request_basis") or {}
        self._trace_job(job["id"], "request", detail=("角色工具语义判断：" + basis.get("summary", "")) if basis.get("source") == "model_tool" else "明确请求 / 主动机会，角色已同意")
        self._trace_job(job["id"], "role", "photo" if mode == "persona" else mode, caption)
        self._trace_job(job["id"], "conditions", detail=json.dumps(agreed, ensure_ascii=False) if agreed else "未设置额外拍摄条件")
        self._photo_note(self.storage.job(job["id"]))
        success = False
        quota_token = None
        failure_stage = "reference"
        try:
            self._trace_job(job["id"], "reference", "warning" if reference_warning else "ok", reference_warning or f"使用 {len(reference_names)} 张真实参考图")
            failure_stage = "quota"
            async with self._generation_slot(source):
                guard_reason = await self._delivery_guard(env=env, job=job)
                if guard_reason:
                    raise ValueError(guard_reason)
                allowed, reason, quota_token = self.moderation.reserve(user_key, raw + "\n" + positive, is_admin=is_admin)
                if not allowed:
                    raise ValueError(reason)
                self._trace_job(job["id"], "quota", detail="生成额度已预留")
                if self.storage.job(job["id"]).get("cancel_requested"):
                    raise asyncio.CancelledError()
                if source in {"active", "morning"}:
                    budget = self.storage.reserve_budget("photos", self.storage.settings["proactive_budget"])
                    if not budget:
                        raise ValueError("今日主动照片预算已用完")
                    self.storage.finish_budget(budget, used=True)
                generating = self.storage.update_job(job["id"], {"status": "generating", "phase": "dispatching"})
                self._photo_note(generating)
                self._trace_job(job["id"], "provider", "running", provider.name)
                failure_stage = "provider"
                effective_timeout = self._timeouts(provider, options)[2]
                try:
                    result = await asyncio.wait_for(provider.generate(positive, negative, reference=reference, options=options), timeout=effective_timeout)
                except asyncio.TimeoutError as exc:
                    raise ProviderError(f"生图请求超时（有效上限 {effective_timeout} 秒，接口与任务上限取较短值）") from exc
                self.storage.update_job(job["id"], {"request_summary": self._request_summary(provider, positive, negative, options, len(reference_names), reference_source)})
                if self.storage.job(job["id"]).get("cancel_requested"):
                    raise asyncio.CancelledError()
                failure_stage = "generation"
                asset = self.storage.save_asset(result.data, result.extension)
                success = True
                guard_reason = await self._delivery_guard(env=env, job=job)
                async with self._lock(env["key"]):
                    session = self.storage.read_session(env["key"])
                    guard_reason = guard_reason or local_delivery_guard(self, env=env, job=job)
                    state_committed = bool(not guard_reason and source != "reply_image" and session and session.get("persona_id") == persona["id"] and session.get("revision", 0) == env["session"].get("revision", 0))
                    if state_committed:
                        if mode == "persona":
                            apply_state(session, patch, self.storage.settings)
                        session["last_image"] = asset.name
                        session["pending"] = {}
                        session = self.storage.save_session(env["key"], session)
                        env["session"] = session
                job = self.storage.update_job(job["id"], {"status": "succeeded", "phase": "generated", "failure_stage": "", "asset": asset.name, "model": result.model, "state_after": (session or {}).get("state", {}), "state_committed": state_committed, "elapsed_ms": int((time.time() - job.get("at", time.time())) * 1000)})
                if guard_reason:
                    job = self.storage.update_job(job["id"], {"delivery_blocked": True, "delivery_note": guard_reason})
                if state_committed:
                    job = self.storage.update_job(job["id"], {"delivery_revision": session.get("revision", 0), "delivery_pending_id": (session.get("pending") or {}).get("request_id", "")})
                job = self._trace_job(job["id"], "generation", detail="图片已验证并保存；" + ("对话配图不修改持续视觉状态" if source == "reply_image" else "状态已提交" if state_committed else "较新状态保留，未覆盖"))
                self._photo_note(job)
                self.storage.append_history({"ok": True, "job_id": job["id"], "image": "/assets/" + asset.name, "caption": caption, "prompt": positive, "negative_prompt": negative, "request_summary": job["request_summary"], "state_patch": patch, "requirements": agreed, "reference_source": reference_source, "trace": job["trace"], "provider": provider.name, "model": result.model, "mode": mode})
                self.storage.cleanup_history(int(options.get("max_history", 100)))
                return job
        except asyncio.CancelledError:
            cancelled = self.storage.update_job(job["id"], {"status": "cancelled", "phase": "cancelled", "error": "用户已撤回本次拍摄" if self.storage.job(job["id"]).get("cancel_requested") else "生成任务被停止"})
            self._photo_note(cancelled)
            raise
        except Exception as exc:
            self.storage.update_job(job["id"], {"request_summary": self._request_summary(provider, positive, negative, options, len(reference_names), reference_source)})
            self._fail_job(job["id"], exc, failure_stage)
            raise
        finally:
            if quota_token is not None:
                self.moderation.finish(user_key, success=success, token=quota_token)

    async def _send_job(self, job, *, event=None, umo=""):
        self._track_current()
        current = self.storage.job(job["id"])
        if current.get("cancel_requested") or current.get("status") == "cancelled":
            raise asyncio.CancelledError()
        guard_reason = await self._delivery_guard(job=current)
        if guard_reason:
            self.storage.update_job(job["id"], {"delivery_blocked": True, "delivery_note": guard_reason})
            self._photo_note(self._trace_job(job["id"], "delivery_guard", "blocked", guard_reason))
            if current.get("delivery_key"):
                self.storage.update_delivery(current["delivery_key"], {"status": "skipped", "retryable": False, "skip_reason": "delivery_guard", "reason": guard_reason})
            raise DeliveryRejectedError(guard_reason)
        destination = str(current.get("umo") or umo or "")
        webchat = self._is_webchat(destination, event=event, job=current)
        use_context = event is None or webchat
        channel = "context" if use_context else "event"
        try:
            chain = MessageChain()
            if job.get("caption"):
                chain.message(job["caption"] + "\n")
            chain.file_image(str(self.storage.asset(job["asset"])))
        except Exception as exc:
            self.storage.update_job(job["id"], {"status": "delivery_failed", "phase": "delivery_failed", "failure_stage": "delivery", "error": "图片已生成，但消息准备失败，尚未发送：" + self._error(exc)})
            self._photo_note(self._trace_job(job["id"], "delivery", "delivery_failed", "消息准备失败：" + self._error(exc)))
            if job.get("delivery_key"):
                self.storage.update_delivery(job["delivery_key"], {"status": "delivery_failed", "error": self._error(exc)})
            raise
        self.storage.update_job(job["id"], {"status": "sending", "phase": "sending", "delivery_blocked": False, "delivery_channel": channel, "delivery_platform": "webchat" if webchat else current.get("delivery_platform", ""), "delivery_evidence": "unverified"})
        if job.get("delivery_key"):
            self.storage.update_delivery(job["delivery_key"], {"status": "sending", "job_id": job["id"]})
        try:
            if use_context:
                send = getattr(self.context, "send_message", None)
                if not destination or not callable(send):
                    raise DeliveryRejectedError("原会话发送通道不可用，照片尚未发送")
                accepted = await send(destination, chain)
            else:
                accepted = await event.send(chain)
            if accepted is False:
                raise DeliveryRejectedError("平台没有接受图片消息")
            if webchat:
                note = "图片已生成，WebChat 会话发送调用已完成，但无法确认实时投递或平台历史保存；请检查原会话后再决定是否补发"
                sent = self.storage.update_job(job["id"], {"status": "uncertain", "phase": "uncertain", "error": note, "failure_stage": "delivery", "delivery_evidence": "unverified"})
                sent = self._trace_job(job["id"], "delivery", "uncertain", note)
                self._photo_note(sent)
                if job.get("delivery_key"):
                    self.storage.update_delivery(job["delivery_key"], {"status": "uncertain", "job_id": job["id"], "error": note})
                return sent
            sent = self.storage.update_job(job["id"], {"status": "sent", "phase": "sent", "error": "", "failure_stage": "", "delivery_evidence": "call_completed"})
            sent = self._trace_job(job["id"], "delivery", detail="发送调用已完成，终端送达未知")
            self._photo_note(sent)
            if job.get("delivery_key"):
                self.storage.update_delivery(job["delivery_key"], {"status": "sent", "job_id": job["id"]})
            return sent
        except DeliveryRejectedError as exc:
            self.storage.update_job(job["id"], {"status": "delivery_failed", "phase": "delivery_failed", "failure_stage": "delivery", "error": "发送未被接受，原图已保留，可补发：" + self._error(exc)})
            self._photo_note(self._trace_job(job["id"], "delivery", "delivery_failed", self._error(exc)))
            if job.get("delivery_key"):
                self.storage.update_delivery(job["delivery_key"], {"status": "delivery_failed", "error": self._error(exc)})
            raise
        except asyncio.CancelledError:
            self.storage.update_job(job["id"], {"status": "uncertain", "phase": "uncertain", "error": "发送被中断，结果不确定，请先检查聊天记录"})
            self._photo_note(self._trace_job(job["id"], "delivery", "uncertain", "发送被中断"))
            if job.get("delivery_key"):
                self.storage.update_delivery(job["delivery_key"], {"status": "uncertain"})
            raise
        except Exception as exc:
            self.storage.update_job(job["id"], {"status": "uncertain", "phase": "uncertain", "error": "图片已生成，发送结果不确定：" + self._error(exc)})
            self._photo_note(self._trace_job(job["id"], "delivery", "uncertain", self._error(exc)))
            if job.get("delivery_key"):
                self.storage.update_delivery(job["delivery_key"], {"status": "uncertain", "error": self._error(exc)})
            raise

    async def _apply_decision(self, env, decision, text, *, event=None, explicit=False, body=None):
        if self._closing:
            raise RuntimeError("插件正在停止")
        guard_reason = await self._delivery_guard(env=env)
        if guard_reason:
            raise ValueError(guard_reason)
        current = self.storage.read_session(env["key"])
        if not current or current.get("persona_id") != env["persona"]["id"]:
            raise ValueError("会话角色已改变，请重新由当前角色判断")
        action = decision["decision"]
        self._prepare_request(env, text, explicit=explicit)
        if env.get("changed_request") and action in {"photo", "scene", "edit", "state"}:
            required = {**requirements((env.get("previous_conditions") or {}).get("requirements")), **infer_requirements(text), **state_patch(decision.get("state_patch")), **requirements(decision.get("requirements")), "notes": text}
            decision = {**decision, "decision": "ask", "reply": "这次要求改变了：" + text[:300] + "。确认保留其他已约定的条件，按这个要求来吗？", "requirements": required, "prompt": ""}
            action = "ask"
        self.storage.record_action({"session_key": env["key"], "persona_id": env["persona"]["id"], "umo": env["umo"], "kind": "decision", "status": action, "request": text, "reply": decision.get("reply", ""), "requirements": decision.get("requirements", {}), "eligible": bool(explicit or self._eligible(text, env)), "trace": [{"stage": "request", "status": "ok" if explicit or self._eligible(text, env) or state_request(text) else "blocked"}, {"stage": "role", "status": action}]})
        if action in {"photo", "scene", "edit"}:
            if not explicit and not self._eligible(text, env):
                return {**decision, "decision": "chat", "blocked": "没有明确的拍摄请求", "reply": "你是在想看照片，还是想聊聊这个？"}
            mode = {"photo": "persona", "scene": "scene", "edit": "edit"}[action]
            agreed = (env["session"].get("pending") or {}).get("requirements", {}) if is_confirmation(text) else {}
            env["approved_requirements"] = {**requirements(decision.get("requirements")), **requirements(agreed)}
            if body is not None:
                # A console click is an explicit request; the role still decides.
                return await self._queue_job(env, decision, text, body)
            if event and not self._claim_photo(event):
                return {"decision": "skip", "reply": "", "duplicate": True}
            reference, lease = await self._capture_reference(event) if event and mode == "edit" else ("", "")
            reference_source = "message_attachment" if reference else "persona" if mode == "persona" else "none"
            if mode == "edit" and not reference:
                reference = env["session"].get("last_image", "")
                reference_source = "last_image" if reference else "none"
            candidate = self.storage.create_job({"status": "queued", "source": "chat", "mode": mode, "umo": env["umo"], "session_key": env["key"], "persona_id": env["persona"]["id"], "reference_asset": reference})
            try:
                pending = env["session"].get("pending") or {}
                if pending and is_confirmation(text) and pending.get("request_kind") == "photo":
                    claim = self.storage.claim_photo_request(env["key"], env["persona"]["id"], pending["request_id"], candidate["id"])
                    if not claim["claimed"]:
                        self.storage.update_job(candidate["id"], {"status": "cancelled", "error": "相同条件已有生成任务", "duplicate_of": claim["job_id"]})
                        return {**decision, "sent": False, "job_id": claim["job_id"], "reused": True}
                    env["session"] = claim["session"]
                    self.storage.update_job(candidate["id"], {"confirmed_request_id": pending["request_id"]})
                job = await self._generate_job(env, decision["prompt"], mode, decision["state_patch"], decision["reply"], text, user_key=f"{event.get_platform_id()}:{event.get_sender_id()}", is_admin=_admin(event), reference_asset=reference, source="chat", job_id=candidate["id"], reference_source=reference_source)
            except Exception as exc:
                if (self.storage.job(candidate["id"]) or {}).get("status") == "queued":
                    self.storage.update_job(candidate["id"], {"status": "failed", "error": self._error(exc)})
                raise
            finally:
                if lease:
                    self.storage.release_asset(lease)
            if _extra(event, "delivery_key"):
                job = self.storage.update_job(job["id"], {"delivery_key": _extra(event, "delivery_key")})
            delivered = await self._send_job(job, event=event)
            sent = delivered.get("status") == "sent"
            if sent:
                await self.dialogue.remember(env, text, decision["reply"] + " [已发送照片：" + decision["prompt"] + "]")
            return {**decision, "sent": sent, "job_id": job["id"], "status": delivered.get("status"), "delivery_evidence": delivered.get("delivery_evidence", "")}
        session = self.storage.read_session(env["key"])
        if action == "ask":
            session["pending"] = pending_request(env, text, decision["reply"], {**infer_requirements(decision["reply"]), **state_patch(decision.get("state_patch")), **requirements(decision.get("requirements"))}, kind="photo" if explicit or self._eligible(text, env) else "state", ttl=int(self.storage.settings["dialogue"].get("confirmation_ttl_sec", 1800)))
        elif action == "state" and (explicit or state_request(text, session.get("pending"))):
            agreed = state_patch((session.get("pending") or {}).get("requirements")) if is_confirmation(text) else {}
            apply_state(session, {**decision["state_patch"], **agreed}, self.storage.settings)
            session["pending"] = {}
        elif action == "refuse":
            session["pending"] = {}
        env["session"] = self.storage.save_session(env["key"], session)
        if event and action != "skip":
            await event.send(MessageChain().message(decision["reply"]))
            await self.dialogue.remember(env, text, decision["reply"])
        elif body is not None and action != "skip":
            await self.dialogue.remember(env, text, decision["reply"])
        return decision

    @filter.command("生图", alias={"拍照"})
    async def command_generate(self, event: AstrMessageEvent, text: str = ""):
        if _extra(event, "handled"):
            return
        _extra(event, "handled", True, set_value=True)
        self._track_current()
        event.should_call_llm(False)
        prompt = text.strip() or re.sub(r"^/?(?:生图|拍照)\s*", "", _text(event))
        if not self.storage.settings["integration"].get("enabled", True):
            yield event.plain_result("随想画卷已停用，请在插件界面开启。")
            return
        if not prompt:
            yield event.plain_result("请描述你想拍摄或绘制的内容，她会先决定是否愿意。")
            return
        try:
            env = await self._env(event)
            self._prepare_request(env, prompt, explicit=True)
            decision = await self.dialogue.decide(env, "用户明确请求拍摄或绘图：" + prompt)
            await self._apply_decision(env, decision, prompt, event=event, explicit=True)
        except Exception as exc:
            yield event.plain_result("这次没能完成：" + self._error(exc))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def natural_route(self, event: AstrMessageEvent):
        if _extra(event, "handled"):
            return
        settings = self.storage.settings["integration"]
        if not settings.get("enabled", True) or settings.get("mode") != "compatibility" or not _addressed(event) or _text(event).startswith("/"):
            return
        env = await self._env(event)
        if cancel_request(_text(event)):
            async for result in self.cancel_photo(event):
                yield result
            return
        self._prepare_request(env, _text(event))
        if not self._eligible(_text(event), env) and not state_request(_text(event), env["session"].get("pending")):
            if env["session"].get("pending"):
                env["session"]["pending"] = {}
                self.storage.save_session(env["key"], env["session"])
            return
        self._track_current()
        _extra(event, "handled", True, set_value=True)
        event.should_call_llm(False)
        try:
            decision = await self.dialogue.decide(env, _text(event))
            await self._apply_decision(env, decision, _text(event), event=event)
        except Exception as exc:
            yield event.plain_result("这次没能完成：" + self._error(exc))

    @filter.command("生图控制台", alias={"人设控制台"})
    async def command_webui(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        if not _admin(event):
            yield event.plain_result("只有管理员可以打开随想画卷控制台。")
        elif self.webui:
            yield event.plain_result(f"控制台：{self.webui.url}\n访问令牌：{self.webui.token}")
        else:
            yield event.plain_result("请在 AstrBot WebUI 的插件详情中打开“随想画卷”。如需旧版独立页面，可开启兼容 WebUI 后重载。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def observe_private(self, event: AstrMessageEvent):
        self.reply_images.observe(event)
        if self.active:
            self.active.observe(event)

    async def _llm_providers(self):
        items = []
        for provider in self.context.get_all_providers():
            meta = provider.meta()
            item = {"id": str(meta.id), "model": str(meta.model or ""), "type": str(meta.type), "models": [], "error": ""}
            # Model enumeration is opt-in; loading the console does no network IO.
            item["models"] = [item["model"]] if item["model"] else []
            items.append(item)
        manager = getattr(self.context, "persona_manager", None)
        personas = []
        if manager:
            for persona in getattr(manager, "personas", []):
                personas.append({"id": str(field(persona, "persona_id", "")), "name": str(field(persona, "persona_id", ""))})
        return {"items": items, "selected": copy.deepcopy(self.storage.settings["llm"]), "personas": personas}

    async def _test_llm(self, body):
        provider = self.context.get_provider_by_id(body["provider_id"]) if body.get("provider_id") else await self.dialogue.provider()
        if provider is None:
            raise ValueError("找不到聊天模型")
        kwargs = {"prompt": "Reply PONG", "system_prompt": "Connectivity test", "contexts": []}
        if body.get("model"):
            kwargs["model"] = str(body["model"])
        started = time.monotonic()
        response = await asyncio.wait_for(provider.text_chat(**kwargs), 30)
        return {"ok": True, "provider_id": str(provider.meta().id), "model": str(body.get("model") or provider.meta().model), "text": str(response.completion_text)[:200], "elapsed_ms": int((time.monotonic() - started) * 1000)}

    async def _image_models(self, body):
        return await self._provider(body.get("name")).list_models()

    async def _image_test(self, body):
        provider = self._provider(body.get("name"))
        if not body.get("generate_image"):
            return await provider.test_connection()
        prompt = str(body.get("prompt") or "simple blue flower on white background")
        options = copy.deepcopy(self.storage.settings["generation"])
        effective = self._timeouts(provider, options)[2]
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(provider.generate(prompt, "", options=options), effective)
        except Exception as exc:
            reason = f"测试生图超时（有效上限 {effective} 秒）" if isinstance(exc, asyncio.TimeoutError) else self._error(exc)
            return {"ok": False, "provider": provider.name, "error": reason, "request_summary": self._request_summary(provider, prompt, "", options, 0, "none")}
        elapsed = int((time.monotonic() - started) * 1000)
        asset = self.storage.save_asset(result.data, result.extension)
        return {"ok": True, "provider": provider.name, "model": result.model, "elapsed_ms": elapsed, "image": image_data_url(self, asset.name), "request_summary": self._request_summary(provider, prompt, "", options, 0, "none")}

    def _jobs(self, limit=50):
        jobs = self.storage.recent_jobs(limit)
        for job in jobs:
            if job.get("asset"):
                job["image"] = image_data_url(self, job["asset"], thumbnail=True)
        return jobs

    async def web_state(self):
        capabilities, error = self._provider_status()
        return {"persona": copy.deepcopy(self.storage.persona()), "settings": self.storage.safe_settings(), "targets": copy.deepcopy(self.storage.targets["items"]), "history": [], "jobs": [], "sessions": self.storage.list_sessions(), "capabilities": capabilities, "integration": {"web_api": callable(getattr(self.context, "register_web_api", None)), "mode": self.storage.settings["integration"]["mode"], "version": VERSION, "provider_error": error}}

    def _provider_status(self, name=None):
        try:
            return dataclasses.asdict(self._provider(name).capabilities), ""
        except ProviderError as exc:
            # Configuration errors must remain repairable from the console.
            unavailable = ProviderCapabilities(text_to_image=False, automated_generation=False)
            return dataclasses.asdict(unavailable), self._error(exc)

    async def web_personas(self):
        return {"items": copy.deepcopy(self.storage.personas["items"])}

    async def web_providers(self):
        result = []
        for name, config in self.storage.safe_settings()["providers"].items():
            capabilities, error = self._provider_status(name)
            result.append({**config, "name": name, "capabilities": capabilities, "error": error})
        return {"items": result}

    async def web_targets(self):
        items = copy.deepcopy(self.storage.targets["items"])
        scheduler = self.active or ActiveScheduler(self)
        for target in items:
            if target.get("scope") == "group":
                continue
            try:
                target["clock"] = {kind: scheduler._time_context(target, kind) for kind in ("active", "morning")}
            except Exception as exc:
                target["clock_error"] = self._error(exc)
        return {"items": items}

    async def web_inspect_target(self, body):
        target = next((item for item in self.storage.targets["items"] if item.get("umo") == str(body.get("umo") or "")), None)
        if target is None:
            raise ValueError("目标会话不存在")
        return await (self.active or ActiveScheduler(self)).inspect_target(copy.deepcopy(target))

    async def web_sessions(self):
        return {"items": self.storage.list_sessions()}

    async def web_jobs(self):
        return {"items": self._jobs()}

    async def web_jobs_query(self, body):
        from .page_api import preview_descriptor
        items = self.storage.recent_jobs(50)
        for job in items:
            job.pop("image", None)
            job.pop("image_url", None)
            if job.get("asset"):
                job["thumbnail"] = preview_descriptor(self, job["asset"])
        return {"items": items}

    async def web_history_query(self, body):
        from .page_api import preview_descriptor
        result = self.storage.history_page(limit=body.get("limit", 20), before_id=body.get("cursor"))
        for item in result["items"]:
            job = self.storage.job(item.get("job_id", "")) or {}
            item.update({key: job[key] for key in ("status", "umo", "cancel_requested", "asset", "recovery", "recovered") if key in job})
            asset = item.get("asset") or (str(item.get("image", "")).rsplit("/", 1)[-1] if str(item.get("image", "")).startswith("/assets/") else "")
            item.pop("image", None)
            if asset:
                item["thumbnail"] = preview_descriptor(self, asset)
        return result

    async def web_job(self, id):
        job = self.storage.job(id)
        if not job:
            raise ValueError("任务不存在")
        if job.get("asset"):
            job["image"] = image_data_url(self, job["asset"])
        return job

    async def web_history(self):
        items = self.storage.recent_history(20)
        for item in items:
            job = self.storage.job(item.get("job_id", "")) or {}
            item.update({k: job[k] for k in ("status", "umo", "cancel_requested", "asset") if k in job})
            if item.get("image", "").startswith("/assets/"):
                item["image"] = image_data_url(self, item["image"].rsplit("/", 1)[-1], thumbnail=True)
        return {"items": items}

    async def web_save_persona(self, body):
        allowed = {"id", "name", "description", "astrbot_persona_id", "consent_prompt", "positive_prompt", "negative_prompt", "style_prompt", "state", "outfit_pool", "reference_enabled", "reference_asset", "reference_assets"}
        value = {k: v for k, v in body.items() if k in allowed}
        if value.get("reference_asset"):
            self.storage.asset(str(value["reference_asset"]))
        if "state" in value:
            value["state"] = state_patch(value["state"])
        item = self.storage.upsert_persona(value)
        if body.get("set_current"):
            self.storage.settings["current_persona"] = item["id"]
            self.storage.save_settings()
        return item

    async def web_delete_persona(self, body):
        self.storage.delete_persona(str(body.get("id") or ""))
        return await self.web_personas()

    async def web_save_provider(self, body):
        name = str(body.get("name") or "").strip()
        if not re.fullmatch(r"[\w.-]{1,80}", name):
            raise ValueError("接口名称请使用字母、数字、汉字、点、下划线或短横线")
        allowed = {"kind", "endpoint", "model", "api_key", "auth_header", "auth_prefix", "supports_image_edit", "supports_seed", "negative_prompt", "negative_mode", "extra_body", "response_path", "timeout", "allow_image_urls", "allowed_image_hosts", "generation_path", "edit_path", "models_path", "connection_path", "connection_method", "prompt_field", "model_field", "negative_prompt_field", "reference_field", "reference_format", "reference_mime_field", "option_fields", "models_response_path", "supports_sampler", "supported_sizes", "params_version", "strength", "noise", "workflow", "workflow_fields", "output_node"}
        patch = {k: v for k, v in body.items() if k in allowed and (k != "api_key" or v)}
        if patch.get("kind", "openai") not in {"openai", "gemini", "novelai", "custom", "comfyui"}:
            raise ValueError("接口类型无效")
        if "extra_body" in patch and not isinstance(patch["extra_body"], dict):
            raise ValueError("额外参数必须为 JSON 对象")
        old = self.storage.settings["providers"].get(name, {})
        for field in ("workflow", "extra_body"):
            if field in patch and isinstance(patch[field], dict):
                patch[field] = _restore_workflow_secrets(patch[field], old.get(field))
        candidate = {**old, **patch, "name": name}
        if "reset_option_fields" in body and not isinstance(body["reset_option_fields"], bool):
            raise ValueError("reset_option_fields 必须为开关值")
        if body.get("reset_option_fields"):
            candidate.pop("option_fields", None)
        provider_from_config(name, candidate).validate_config()
        self.storage.settings["providers"][name] = candidate
        if body.get("clear_api_key"):
            self.storage.settings["providers"][name]["api_key"] = ""
        if body.get("set_default"):
            self.storage.settings["default_provider"] = name
        self.storage.save_settings()
        return self.storage.safe_settings()["providers"][name]

    async def web_delete_provider(self, body):
        name = str(body.get("name") or "")
        providers = self.storage.settings["providers"]
        if name not in providers:
            raise ValueError("接口不存在")
        if len(providers) == 1:
            raise ValueError("至少保留一个接口")
        del providers[name]
        if self.storage.settings["default_provider"] == name:
            self.storage.settings["default_provider"] = next(iter(providers))
        if self.storage.settings["reply_images"].get("provider") == name:
            self.storage.settings["reply_images"]["provider"] = ""
        self.storage.save_settings()
        return await self.web_providers()

    async def web_save_settings(self, body):
        ranges = {"daily_limit": (0, 10000), "min_interval_sec": (0, 86400), "max_concurrency": (1, 16), "width": (64, 4096), "height": (64, 4096), "steps": (1, 150), "scale": (0, 30), "seed": (-1, 4294967295), "max_history": (1, 5000), "timeout_sec": (5, 300), "context_turns": (1, 100), "confirmation_ttl_sec": (30, 86400), "check_interval_sec": (15, 3600), "min_gap_sec": (60, 604800), "min_idle_sec": (0, 604800), "silence_after": (1, 100), "silence_hours": (1, 720)}
        ranges.update({"outfit_sec": (0, 2592000), "pose_sec": (0, 604800), "expression_sec": (0, 604800), "scene_sec": (0, 2592000), "messages": (0, 10000), "photos": (0, 10000), "llm": (0, 10000), "jitter_percent": (0, 40)})
        ranges["max_concurrent_targets"] = (1, 16)
        ranges["min_interval_sec"] = (0, 86400)
        ranges["default_probability"] = (0, 100)
        ranges["max_attempts"] = (1, 5)
        ranges["retry_interval_sec"] = (60, 3600)
        ranges["max_wait_sec"] = (5, 600)
        allowed_groups = {"integration", "dialogue", "reply_images", "llm", "moderation", "generation", "active", "good_morning", "state_lifetimes", "proactive_budget"}
        candidate = copy.deepcopy(self.storage.settings)
        for group, patch in body.items():
            if group not in allowed_groups or not isinstance(patch, dict):
                continue
            for key, value in patch.items():
                if key not in candidate[group]:
                    continue
                if key in ranges:
                    low, high = ranges[key]
                    if key == "timeout_sec" and group == "generation":
                        high = 600
                    if isinstance(value, bool):
                        raise ValueError(f"{key} 必须为数值")
                    if key in {"max_attempts", "retry_interval_sec"}:
                        number = float(value)
                        if not math.isfinite(number) or not number.is_integer():
                            raise ValueError(f"{key} 必须为整数")
                    value = float(value) if key in {"scale", "default_probability"} else int(value)
                    if not math.isfinite(value) or not low <= value <= high:
                        raise ValueError(f"{key} 应在 {low} 到 {high} 之间")
                elif isinstance(candidate[group][key], bool):
                    if not isinstance(value, bool):
                        raise ValueError(f"{key} 必须为开关值")
                elif key == "timezone":
                    from zoneinfo import ZoneInfo
                    ZoneInfo(str(value))
                elif key in {"start", "end", "default_start", "default_end"}:
                    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value)):
                        raise ValueError("时间格式应为 HH:MM")
                elif key == "mode" and value not in {"native_tools", "compatibility"}:
                    raise ValueError("聊天接入模式无效")
                elif group == "good_morning" and key == "image_mode":
                    if not isinstance(value, str) or value not in {"prefer_photo", "auto", "text_only"}:
                        raise ValueError("早安配图模式无效")
                elif group == "reply_images" and key == "provider":
                    if not isinstance(value, str) or (value and value not in candidate["providers"]):
                        raise ValueError("配图接口不存在，请重新选择")
                candidate[group][key] = value
        candidate["integration"]["strict_trigger"] = True
        for key, section in (("current_persona", self.storage.personas["items"]), ("default_provider", self.storage.settings["providers"])):
            if key in body:
                value = str(body[key])
                valid = value in section if isinstance(section, dict) else any(p["id"] == value for p in section)
                if not valid:
                    raise ValueError("默认角色或接口不存在")
                candidate[key] = value
        self.storage.settings.clear()
        self.storage.settings.update(candidate)
        self.storage.save_settings()
        async with self._slots:
            self._slots.notify_all()
        return self.storage.safe_settings()

    async def web_simulate(self, body):
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("请输入对话内容")
        env = await self.dialogue.environment(str(body.get("umo") or "webui:preview"), str(body.get("persona_id") or ""))
        self._prepare_request(env, text)
        decision = await self.dialogue.decide(env, text)
        if env.get("changed_request") and decision["decision"] in {"photo", "scene", "edit", "state"}:
            decision.update(decision="ask", reply="要求改变后需要重新确认，本轮不执行。", prompt="")
        decision["eligible"] = self._eligible(text, env)
        decision["executed"] = False
        return decision

    async def _queue_job(self, env, decision, text, body, *, event=None, retry_old=None):
        mode = {"photo": "persona", "scene": "scene", "edit": "edit"}[decision["decision"]]
        requested_mode = str(body.get("mode") or "persona")
        if requested_mode == "scene" and mode == "persona":
            raise ValueError("角色决策与场景模式不一致，请明确要求普通场景绘图")
        options = {k: body[k] for k in ("width", "height", "steps", "scale", "seed", "sampler", "aspect_ratio", "image_size", "timeout_sec") if body.get(k) is not None}
        options["_explicit"] = [key for key in body.get("_explicit", list(options)) if key in options]
        source = "reply_image" if retry_old and retry_old.get("source") == "reply_image" else "chat" if event else "webui"
        reference = str(body.get("reference_asset") or (env["session"].get("last_image") if mode == "edit" else "") or "")
        reference_source = str(body.get("reference_source") or ("explicit_reference" if reference else "persona" if mode == "persona" else "none"))
        prepared = self._prepare_generation(env, decision["prompt"], mode, decision["state_patch"], decision["reply"], text, reference_asset=reference, provider_name=str(body.get("provider") or ""), options=options, source=source, reference_source=reference_source, explicit_retry=bool(retry_old))
        selected_provider = prepared["provider"].name
        job = self.storage.create_job({"status": "queued", "phase": "local_queued", "preflight_checked": True, "provider": selected_provider, "request_summary": self._request_summary(prepared["provider"], prepared["positive"], prepared["negative"], prepared["options"], len(prepared["reference_names"]), reference_source), "source": source, "raw": text, "caption": decision["reply"], "mode": mode, "umo": env["umo"], "session_key": env["key"], "persona_id": env["persona"]["id"], "reference_asset": reference, "reference_assets": env.get("approved_reference_assets", []), "reference_source": reference_source, "request_basis": env.get("request_basis", {})})
        if self._is_webchat(env["umo"], event=event, job=retry_old):
            self.storage.update_job(job["id"], {"delivery_platform": "webchat"})
        if retry_old:
            self.storage.update_job(job["id"], {"retry_of": retry_old["id"], "confirmed_request_id": retry_old.get("confirmed_request_id", "")})
            try:
                result = self.storage.claim_job_retry(retry_old["id"], job["id"])
            except Exception:
                self.storage.update_job(job["id"], {"status": "cancelled", "error": "重试授权已改变，未提交接口"})
                raise
            if not result["claimed"]:
                self.storage.update_job(job["id"], {"status": "cancelled", "error": "原请求已有重试任务", "duplicate_of": result["job_id"]})
                return {"job_id": result["job_id"], "status": result["job"].get("status"), "reused": True}
            env["session"] = result["session"]
        else:
            pending = env["session"].get("pending") or {}
            if pending and pending.get("request_kind") == "photo" and is_confirmation(text):
                try:
                    result = self.storage.claim_photo_request(env["key"], env["persona"]["id"], pending["request_id"], job["id"])
                except Exception:
                    self.storage.update_job(job["id"], {"status": "cancelled", "error": "拍摄条件已改变，未提交接口"})
                    raise
                if not result["claimed"]:
                    self.storage.update_job(job["id"], {"status": "cancelled", "error": "相同条件已有生成任务", "duplicate_of": result["job_id"]})
                    return {"job_id": result["job_id"], "status": (self.storage.job(result["job_id"]) or {}).get("status"), "reused": True}
                env["session"] = result["session"]
                self.storage.update_job(job["id"], {"confirmed_request_id": pending["request_id"]})
        self._photo_note(job)
        async def run():
            try:
                generated = await self._generate_job(env, decision["prompt"], mode, decision["state_patch"], decision["reply"], text, reference_asset=reference, provider_name=selected_provider, options=options, source=source, is_admin=_admin(event) if event else True, user_key=f"{event.get_platform_id()}:{event.get_sender_id()}" if event else "webui", job_id=job["id"], reference_source=reference_source)
                if event:
                    await self._send_job(generated, umo=generated["umo"])
            except Exception as exc:
                self._fail_job(job["id"], exc)
                if event:
                    await self._notify_photo_failure(self.storage.job(job["id"]), event=event)
        self._spawn(run(), "persona-canvas-web-job", job["id"])
        return {"job_id": job["id"], "status": "queued", "phase": "local_queued", "preflight_checked": True, "submitted": False, "upstream_accepted": False, "provider": selected_provider, "decision": decision, "caption": decision["reply"]}

    async def web_generate(self, body):
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("请输入拍摄描述")
        env = await self.dialogue.environment(str(body.get("umo") or "webui:studio"), str(body.get("persona_id") or ""))
        self._prepare_request(env, text, explicit=True)
        mode = str(body.get("mode") or "persona")
        directive = {"persona": "用户在拍摄工作台明确请求你的人设照片：", "scene": "用户明确请求普通场景绘图，不加入人设：", "edit": "用户明确请求编辑参考图片："}.get(mode)
        if not directive:
            raise ValueError("拍摄模式无效")
        decision = await self.dialogue.decide(env, directive + text)
        return await self._apply_decision(env, decision, text, explicit=True, body=body)

    async def web_retry_job(self, body):
        return await self._retry_job(body)

    async def web_resend_job(self, body):
        return await self._resend_job(body)

    async def web_recover_job(self, body):
        """Retrieve an existing workflow result; never submit or send a photo."""
        self._track_current()
        identifier = str(body.get("id") or "")
        async with self._lock("recovery:" + identifier):
            job = self.storage.job(identifier) or {}
            if job.get("asset"):
                self.storage.asset(job["asset"])
                return {"ok": True, "job_id": identifier, "status": job["status"], "recovery_status": "completed", "generated": False, "image_saved": True, "reused": True, "note": "原图已保存，未重新生图或发送"}
            record = job.get("recovery") or {}
            if not record.get("prompt_id"):
                raise ValueError("没有可靠的 ComfyUI 任务编号，无法查询原结果；不会自动重新提交")
            if job.get("status") in {"queued", "deciding", "generating", "sending"}:
                raise ValueError("原任务仍在本地执行，请等待结果或结束等待后再检查")
            provider = self._provider(job.get("provider"))
            recover = getattr(provider, "recover", None)
            if not callable(recover):
                raise ValueError("当前接口不支持原工作流结果查询")
            result = await recover(record["prompt_id"], fingerprint=record.get("fingerprint", ""))
            state = {**record, **getattr(provider, "last_recovery", {})}
            phase = str(state.get("phase") or state.get("status") or "missing")
            if result is None:
                self.storage.update_job(identifier, {"recovery": state})
                note = {"queued": "原工作流仍在排队", "running": "原工作流仍在执行", "missing": "尚未找到原任务结果；记录可能已清理，不代表未执行"}.get(phase, "尚无可取回图片")
                return {"ok": True, "job_id": identifier, "status": job["status"], "recovery_status": phase, "generated": False, "image_saved": False, "note": note}
            asset = self.storage.save_asset(result.data, result.extension)
            current = self.storage.job(identifier)
            updates = {"asset": asset.name, "model": result.model, "recovery": {**state, "phase": "completed", "saved": True}, "recovered": True, "delivery_blocked": True, "delivery_note": "已取回原工作流图片，仅供查看或下载；未更新视觉状态，未发送", "state_committed": False}
            if current.get("status") != "cancelled" and not current.get("cancel_requested"):
                updates.update(status="succeeded", phase="recovered", error="")
            job = self.storage.update_job(identifier, updates)
            self._photo_note(self._trace_job(identifier, "recovery", "completed", updates["delivery_note"]))
            self.storage.append_history({"ok": True, "job_id": identifier, "image": "/assets/" + asset.name, "caption": job.get("caption", ""), "mode": job.get("mode", "persona"), "provider": job.get("provider"), "recovered": True})
            return {"ok": True, "job_id": identifier, "status": job["status"], "recovery_status": "completed", "generated": False, "image_saved": True, "note": updates["delivery_note"]}

    async def _resend_job(self, body, *, event=None):
        identifier = str(body.get("id") or "")
        observed = self.storage.job(identifier) or {}
        expected_count = int(observed.get("resend_count", 0))
        if observed.get("status") == "sending":
            raise ValueError("照片正在发送，请等待结果并检查聊天记录，不能同时补发")
        # All entry points share the lock and re-read after waiting, so concurrent
        # clicks cannot enqueue two copies of the same saved image.
        async with self._lock("delivery:" + identifier):
            job = self.storage.job(identifier) or {}
            if int(job.get("resend_count", 0)) != expected_count:
                return {"ok": job.get("status") == "sent", "job_id": identifier, "status": job.get("status"), "reused": True, "error": job.get("error", "")}
            if not job.get("asset") or job.get("cancel_requested"):
                raise ValueError("任务没有可补发照片或用户已撤回请求")
            session = self.storage.read_session(job["session_key"])
            if not session or session.get("persona_id") != job.get("persona_id"):
                raise ValueError("当前会话角色已改变，请重新由当前角色判断")
            recover_sent = job.get("status") == "sent" and body.get("confirm_uncertain") is True
            if recover_sent and job.get("source") not in {"chat", "active", "morning", "reply_image"}:
                raise ValueError("该任务没有可核对的聊天发送来源，不能恢复补发")
            if job.get("status") == "sent" and not recover_sent:
                return {"ok": True, "job_id": identifier, "status": "sent", "reused": True}
            if job.get("status") not in {"delivery_failed", "uncertain"} and not recover_sent:
                raise ValueError("仅能补发明确拒收或发送结果不确定的照片，正在发送时请等待")
            if job["status"] == "uncertain" and body.get("confirm_uncertain") is not True:
                raise ValueError("请先检查聊天记录，确认没有收到照片后再明确补发")
            destination = str(job.get("umo") or "")
            if not destination or destination.startswith("webui:"):
                raise ValueError("工作台照片没有聊天发送目标，请直接查看或下载原图")
            self.storage.asset(job["asset"])
            self.storage.update_job(identifier, {"resend_count": int(job.get("resend_count", 0)) + 1})
            self._trace_job(identifier, "resend", "requested", "用户已检查聊天记录并确认未收到，恢复补发已保存原图" if recover_sent else "用户明确补发已保存原图，不重新生成")
            try:
                await self._send_job(job, umo=destination)
            except Exception as exc:
                current = self.storage.job(identifier)
                return {"ok": False, "job_id": identifier, "status": current["status"], "error": current.get("error") or self._error(exc)}
            current = self.storage.job(identifier)
            return {"ok": current["status"] == "sent", "job_id": identifier, "status": current["status"], "generated": False, "delivery_evidence": current.get("delivery_evidence", ""), "error": current.get("error", "")}

    async def _retry_job(self, body, *, event=None):
        old = self.storage.job(str(body.get("id") or ""))
        if not old or old.get("status") not in {"failed", "cancelled"} or not old.get("persona_snapshot"):
            raise ValueError("仅能重试已失败的生成任务；发送结果不确定时请先检查聊天记录")
        if old.get("cancel_requested"):
            raise ValueError("拍摄意愿已撤回，请在生图工作台重新提交，由角色重新判断")
        if old.get("recovery", {}).get("submitted"):
            raise ValueError("工作流可能已受理，请先检查原任务结果；再次绘图需作为新请求提交，不重复原任务")
        persona = copy.deepcopy(old["persona_snapshot"])
        session = self.storage.read_session(old["session_key"])
        if not session or session.get("persona_id") != persona["id"]:
            raise ValueError("当前角色已改变，不能重用旧角色的拍摄授权，请重新提交请求")
        existing = self.storage.job(old.get("retry_job_id", "")) or {}
        if existing.get("status") in {"queued", "deciding", "generating", "succeeded", "sending", "sent", "uncertain", "delivery_failed"}:
            return {"job_id": existing["id"], "status": existing["status"], "reused": True, "instruction": "原请求已有重试任务，不再重复提交"}
        pending = session.get("pending") or {}
        if pending and (not old.get("confirmed_request_id") or pending.get("request_id") != old["confirmed_request_id"]):
            raise ValueError("拍摄条件已更新，请先确认当前条件，不能重用旧授权")
        if old.get("confirmed_request_id") and not pending:
            raise ValueError("原拍摄条件已撤回或移除，请重新由角色判断")
        if pending.get("execution_job_id") and pending["execution_job_id"] != old["id"]:
            existing = self.storage.job(pending["execution_job_id"]) or {}
            return {"job_id": pending["execution_job_id"], "status": existing.get("status"), "reused": True, "instruction": "原请求已有重试任务，不再重复提交"}
        persona["state"] = copy.deepcopy(session["state"])
        env = {"umo": old["umo"], "key": old["session_key"], "persona": persona, "session": session, "approved_requirements": old.get("requirements", {}), "approved_reference_assets": old.get("reference_assets", []), "request_basis": {"source": "model_tool" if event else "webui_retry", "summary": "用户明确重试失败任务 " + old["id"]}}
        env.update(old.get("identity") or await self.dialogue.current_identity(old["umo"]))
        decision = {"decision": {"persona": "photo", "scene": "scene", "edit": "edit"}[old["mode"]], "prompt": old["request_prompt"], "reply": old["caption"], "state_patch": old["state_patch"]}
        result = await self._queue_job(env, decision, old["raw"], {**old.get("options", {}), "reference_asset": old.get("reference_asset", ""), "reference_source": old.get("reference_source", "explicit_reference"), "provider": old.get("provider", ""), "mode": old["mode"]}, event=event, retry_old=old)
        return result

    async def web_cancel_job(self, body):
        return await self._cancel_job(str(body.get("id") or ""))

    async def web_diagnostics(self):
        settings = self.storage.settings
        selected = str(settings["llm"].get("provider_id") or "")
        default = settings["providers"].get(settings["default_provider"], {})
        steps = [
            {"name": "聊天模型", "ok": (self.context.get_provider_by_id(selected) is not None if selected else settings["llm"].get("fallback_to_current", True)), "message": "沿用会话聊天模型，使用测试 LLM 验证" if not selected else selected + ("；不存在时允许回退" if settings["llm"].get("fallback_to_current", True) else "；禁用回退"), "page": "providers"},
            {"name": "绘画接口", "ok": bool(default.get("endpoint") and (default.get("workflow") if default.get("kind") == "comfyui" else default.get("model")) and not self._provider_status()[1]), "message": "检查地址与模型 / API 工作流；连接与测试图需主动验证", "page": "providers"},
            {"name": "角色外观", "ok": bool(self.storage.persona().get("positive_prompt") or self.storage.persona().get("reference_asset")), "message": "填写固定外观或上传角色参考图", "page": "persona"},
            {"name": "工具启用", "ok": None, "message": "原生模式需在 AstrBot 允许 state、photo、conditions、control 四个工具；兼容模式不需要工具调用", "page": "settings"},
        ]
        return {"items": self.storage.recent_actions(limit=100), "budget": self.storage.budget_summary(settings["proactive_budget"]), "setup": steps}

    async def web_upload_reference(self, data, name="reference", persona_id=""):
        if not isinstance(data, bytes) or len(data) > 16 * 1024 * 1024 or not data:
            raise ValueError("参考图大小应在 1 字节到 16 MB 之间")
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 32_000_000:
                raise ValueError("请选择有效的 PNG、JPEG 或 WebP 图片，像素总量不超过 3200 万")
            extension = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[image.format]
            image.verify()
        asset = self.storage.save_asset(data, extension)
        result = {"asset": asset.name, "image": image_data_url(self, asset.name)}
        if persona_id:
            persona = copy.deepcopy(self.storage.persona(persona_id))
            if persona["id"] != persona_id:
                raise ValueError("角色不存在")
            persona.update(reference_asset=asset.name, reference_enabled=True)
            result["persona"] = self.storage.upsert_persona(persona)
        return result

    async def web_target_action(self, body):
        action, umo = str(body.get("action") or ""), str(body.get("umo") or "")
        target = next((t for t in self.storage.targets["items"] if t.get("umo") == umo), None)
        if not target:
            raise ValueError("目标会话不存在，请先在对应私聊或群聊中发送一条消息")
        if action == "remove":
            self.storage.targets["items"].remove(target)
        elif action == "toggle":
            if target.get("scope") == "group":
                raise ValueError("群聊只支持对话配图，不启用主动联系")
            target["enabled"] = not target.get("enabled", False)
        elif action == "test":
            if target.get("scope") == "group":
                raise ValueError("群聊不发送主动测试消息，请通过对话测试配图")
            accepted = await self.context.send_message(umo, MessageChain().message("这是随想画卷的连接测试消息。"))
            if accepted is False:
                raise ValueError("平台没有接受测试消息")
        elif action == "save":
            patch = body.get("patch") or body
            allowed = {"enabled", "persona_id", "start", "end", "timezone", "min_gap_sec", "min_idle_sec", "silence_after", "with_image", "morning_enabled", "morning_start", "morning_end", "reply_image_enabled", "reply_image_probability"}
            candidate = copy.deepcopy(target)
            for key, value in patch.items():
                if key in allowed:
                    if key == "reply_image_probability":
                        if isinstance(value, bool):
                            raise ValueError("配图概率必须为 0–100 的数值")
                        value = float(value)
                        if not math.isfinite(value) or not 0 <= value <= 100:
                            raise ValueError("配图概率必须在 0–100% 之间")
                    elif key in {"min_gap_sec", "min_idle_sec", "silence_after"}:
                        value = int(value)
                        if value < (1 if key == "silence_after" else 0):
                            raise ValueError("间隔和未回复次数不能为负数")
                    elif key in {"start", "end", "morning_start", "morning_end"} and value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value)):
                        raise ValueError("时间格式应为 HH:MM")
                    elif key in {"enabled", "with_image", "morning_enabled", "reply_image_enabled"} and not isinstance(value, bool):
                        raise ValueError("目标开关必须为布尔值")
                    elif key == "timezone" and value:
                        from zoneinfo import ZoneInfo
                        ZoneInfo(str(value))
                    elif key == "persona_id" and value and not any(p["id"] == value for p in self.storage.personas["items"]):
                        raise ValueError("绑定的角色不存在")
                    candidate[key] = value
            if target.get("scope") == "group" and (candidate.get("enabled") or candidate.get("morning_enabled")):
                raise ValueError("群聊只支持对话配图，不能启用主动联系或早安")
            target.clear()
            target.update(candidate)
        else:
            raise ValueError("目标操作无效")
        self.storage.save_targets()
        return await self.web_targets()

    async def web_export(self):
        return self.storage.export(include_secrets=False)
