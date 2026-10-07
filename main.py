from __future__ import annotations

import asyncio
import base64
import copy
import dataclasses
import io
import json
import re
import time
from contextlib import asynccontextmanager

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from .active import ActiveScheduler
from .dialogue import Dialogue, field, maybe_await
from .companion import apply_state, cancel_request, infer_requirements, is_confirmation, pending_request, requirements, valid_pending
from .intent import Intent, photo_request, prompt_bundle, state_patch, state_request
from .moderation import Moderation
from .page_api import CanvasPageApi, image_data_url
from .providers.base import ProviderCapabilities, ProviderError, provider_from_config
from .storage import Storage
from .webui_server import WebUI

PLUGIN_NAME = "astrbot_plugin_persona_canvas"
VERSION = "0.5.1"


def _text(event):
    return str(getattr(event, "message_str", "") or "").strip()


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
        self.page_api = CanvasPageApi(self)
        self.webui = None
        self.active = None
        self._tasks = set()
        self._requests = set()
        self._job_tasks = {}
        self._session_locks = {}
        self._slots = asyncio.Condition()
        self._running = 0
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
            self.storage.record_action({"kind": "photo", "job_id": job["id"], "session_key": job["session_key"], "persona_id": job.get("persona_id"), "umo": job.get("umo"), "status": job["status"], "summary": job.get("request_prompt") or job.get("raw", ""), "visual_state": {**job.get("persona_snapshot", {}).get("state", {}), **job.get("state_patch", {})} if job.get("mode") == "persona" else {}, "requirements": job.get("requirements", {}), "mode": job.get("mode"), "error": job.get("error", ""), "trace": job.get("trace", [])}, "photo:" + job["id"])

    def _prepare_request(self, env, text, *, explicit=False):
        pending = env["session"].get("pending") or {}
        if pending and not valid_pending(pending, env):
            session = self.storage.session(env["key"], env["persona"]["id"])
            session["pending"] = {}
            env["session"] = self.storage.save_session(env["key"], session)
        elif pending and not is_confirmation(text) and (explicit or photo_request(text, pending) or state_request(text, pending)):
            env["changed_request"] = True
            env["previous_conditions"] = pending

    def _save_conditions(self, env, text, reply, required=None, kind="photo"):
        session = self.storage.session(env["key"], env["persona"]["id"])
        if env.get("changed_request"):
            required = {**requirements((env.get("previous_conditions") or {}).get("requirements")), **infer_requirements(text), **requirements(required)}
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
        _extra(event, "handled", True, set_value=True)
        event.should_call_llm(False)
        env = await self._env(event)
        cancelled = 0
        for job_id in list(self._job_tasks):
            job = self.storage.job(job_id)
            if job and job.get("session_key") == env["key"] and job.get("status") in {"queued", "deciding", "generating", "succeeded"}:
                await self._cancel_job(job_id)
                cancelled += 1
        session = self.storage.session(env["key"], env["persona"]["id"])
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
        for job in self.storage.recent_jobs(10000):
            if job.get("status") in {"queued", "deciding", "generating"}:
                self.storage.update_job(job["id"], {"status": "failed", "error": "任务被重启中断，可在任务历史中重试"})
                self._photo_note(self.storage.job(job["id"]))
            elif job.get("status") == "sending":
                self.storage.update_job(job["id"], {"status": "uncertain", "error": "重启前正在发送，结果不确定，请先检查聊天记录"})
                self._photo_note(self.storage.job(job["id"]))
        for delivery in self.storage.export().get("deliveries", []):
            if delivery.get("status") in {"claimed", "decided", "generating"}:
                self.storage.update_delivery(delivery["key"], {"status": "failed", "error": "重启中断，尚未发送，将有限重试", "retry_at": time.time() + 300})
            elif delivery.get("status") == "sending":
                self.storage.update_delivery(delivery["key"], {"status": "uncertain", "error": "重启前正在发送，请先检查聊天记录"})

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
        text = str(exc)[:1500]
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
        return provider_from_config(key, copy.deepcopy(config))

    @asynccontextmanager
    async def _generation_slot(self):
        async with self._slots:
            await self._slots.wait_for(lambda: self._closing or self._running < int(self.storage.settings["generation"].get("max_concurrency", 2)))
            if self._closing:
                raise RuntimeError("插件正在停止")
            self._running += 1
        try:
            yield
        finally:
            async with self._slots:
                self._running -= 1
                self._slots.notify_all()

    def _lock(self, key):
        return self._session_locks.setdefault(key, asyncio.Lock())

    async def _env(self, event):
        cached = _extra(event, "environment")
        return cached or await self.dialogue.environment(event.unified_msg_origin)

    def _eligible(self, text, env):
        pending = {**(env["session"].get("pending") or {}), "last_image": env["session"].get("last_image")}
        return photo_request(text, pending)

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
                        return result["asset"]
        return ""

    @filter.on_llm_request()
    async def inject_visual_state(self, event: AstrMessageEvent, req):
        if not self.storage.settings["integration"].get("enabled", True):
            return
        env = await self.dialogue.environment(event.unified_msg_origin, conversation=getattr(req, "conversation", None))
        self._prepare_request(env, _text(event))
        if env["session"].get("pending") and not self._eligible(_text(event), env) and not state_request(_text(event), env["session"].get("pending")):
            env["session"]["pending"] = {}
            env["session"] = self.storage.save_session(env["key"], env["session"])
        _extra(event, "environment", env, set_value=True)
        if not _extra(event, "reference_checked"):
            _extra(event, "reference_checked", True, set_value=True)
            try:
                _extra(event, "reference_asset", await self._capture_reference(event), set_value=True)
            except Exception as exc:
                _extra(event, "reference_error", self._error(exc), set_value=True)
        req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n" + self.dialogue.visual_prompt(env)

    @filter.on_llm_response()
    async def remember_conditions(self, event: AstrMessageEvent, response):
        if not self.storage.settings["integration"].get("enabled", True) or _extra(event, "photo_attempted") or _extra(event, "conditions_recorded"):
            return
        env = await self._env(event)
        text = str(getattr(response, "completion_text", "") or "")
        session = self.storage.session(env["key"], env["persona"]["id"])
        pending = session.get("pending") or {}
        if self._eligible(_text(event), env) or state_request(_text(event), pending):
            status = "refuse" if re.search(r"不想拍|不愿意拍|不拍了|不能拍|拒绝", text) else "ask" if re.search(r"可以吗|行吗|好吗|好不好|要不要|愿意吗", text) else "chat"
            self.storage.record_action({"kind": "decision", "source": "native", "session_key": env["key"], "persona_id": env["persona"]["id"], "umo": env["umo"], "request": _text(event), "reply": text[:2000], "status": status, "trace": [{"stage": "role", "status": status, "detail": "角色回复，未调用拍照工具；标签依据回复文本归纳"}]})
        if pending and not self._eligible(_text(event), env) and not state_request(_text(event), pending):
            session["pending"] = {}
            session = self.storage.save_session(env["key"], session)
            pending = {}
        if re.search(r"不想拍|不愿意拍|不拍了|不能拍|不要发|别发", _text(event) + "\n" + text):
            if pending:
                session["pending"] = {}
                self.storage.save_session(env["key"], session)
        elif (self._eligible(_text(event), env) or state_request(_text(event), pending)) and re.search(r"可以吗|行吗|好吗|好不好|要不要|愿意吗", text):
            self._save_conditions(env, _text(event), text, infer_requirements(text), "photo" if self._eligible(_text(event), env) else "state")
        elif re.search(r"(?:给你|发你|给你看|要不要)[^。！？\n]{0,20}(?:照片|自拍|拍一张)", text) and re.search(r"可以吗|好吗|要不要|想看吗|好不好", text):
            self._save_conditions(env, "角色主动提议拍照，等待用户确认", text, infer_requirements(text))

    @filter.llm_tool(name="persona_canvas_conditions")
    async def tool_conditions(self, event: AstrMessageEvent, reply: str, request_kind: str = "photo", outfit: str = "", camera: str = "", pose: str = "", expression: str = "", scene: str = "", avoid: str = ""):
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
        """
        env = await self._env(event)
        if not self.storage.settings["integration"].get("enabled", True) or not _addressed(event) or request_kind not in {"photo", "state"} or not (self._eligible(_text(event), env) or state_request(_text(event), env["session"].get("pending"))):
            return json.dumps({"ok": False, "reason": "当前没有明确的拍摄或状态请求"}, ensure_ascii=False)
        if _extra(event, "photo_attempted") or not reply.strip():
            return json.dumps({"ok": False, "reason": "本轮已安排拍摄或缺少确认问题"}, ensure_ascii=False)
        pending = self._save_conditions(env, _text(event), reply, {"outfit": outfit, "camera": camera, "pose": pose, "expression": expression, "scene": scene, "avoid": avoid}, request_kind)
        _extra(event, "conditions_recorded", True, set_value=True)
        self.storage.record_action({"session_key": env["key"], "persona_id": env["persona"]["id"], "umo": env["umo"], "kind": "decision", "status": "ask", "request": _text(event), "reply": reply, "requirements": pending["requirements"]})
        return json.dumps({"ok": True, "request_id": pending["request_id"], "instruction": "现在向用户询问这些条件。本轮不得拍照或更新状态，等下一条明确确认。"}, ensure_ascii=False)

    @filter.llm_tool(name="persona_canvas_state")
    async def tool_state(self, event: AstrMessageEvent, outfit: str = "", pose: str = "", expression: str = "", scene: str = ""):
        """你愿意接受用户明确提出的换装/姿势要求时，更新会话视觉状态，不会拍照。拒绝或提条件时不要调用。

        Args:
            outfit (string): 新服装，未改变留空。
            pose (string): 新姿势，未改变留空。
            expression (string): 新表情，未改变留空。
            scene (string): 新场景，未改变留空。
        """
        self._track_current()
        env = await self._env(event)
        self._prepare_request(env, _text(event))
        if not self.storage.settings["integration"].get("enabled", True) or not _addressed(event) or not state_request(_text(event), env["session"].get("pending")):
            return json.dumps({"ok": False, "reason": "用户没有明确要求更新状态"}, ensure_ascii=False)
        if _extra(event, "conditions_recorded") or env.get("changed_request"):
            return json.dumps({"ok": False, "reason": "要求改变或正在询问条件，请先重新确认"}, ensure_ascii=False)
        patch = state_patch({k: v for k, v in {"outfit": outfit, "pose": pose, "expression": expression, "scene": scene}.items() if v})
        if is_confirmation(_text(event)):
            patch.update(state_patch((env["session"].get("pending") or {}).get("requirements")))
        async with self._lock(env["key"]):
            session = self.storage.session(env["key"], env["persona"]["id"])
            apply_state(session, patch, self.storage.settings)
            session["pending"] = {}
            session = self.storage.save_session(env["key"], session)
            env["session"] = session
            env["persona"]["state"] = session["state"]
        return json.dumps({"ok": True, "state": session["state"], "photo_generated": False}, ensure_ascii=False)

    @filter.llm_tool(name="persona_canvas_photo")
    async def tool_photo(self, event: AstrMessageEvent, prompt: str, mode: str = "persona", caption: str = "", outfit: str = "", pose: str = "", expression: str = "", scene: str = "", use_last_image: bool = False):
        """用户明确请求照片/绘图或确认了拍摄条件，并且你本人愿意时才执行。普通聊天、否定、引用、拒绝、提条件时不得调用。一次请求一次图片。

        Args:
            prompt (string): 你同意的拍摄描述，体现镜头、动作与情绪，保持固定长相。
            mode (string): persona 人设自拍，scene 普通场景绘图，edit 真实参考图编辑。
            caption (string): 图片附带的简短角色台词。
            outfit (string): 本次持续换装，未改变留空。
            pose (string): 新姿势，未改变留空。
            expression (string): 新表情，未改变留空。
            scene (string): 新场景，未改变留空。
            use_last_image (boolean): 编辑上一张图片时为 true。
        """
        self._track_current()
        env = await self._env(event)
        self._prepare_request(env, _text(event))
        if not self.storage.settings["integration"].get("enabled", True) or not _addressed(event) or not self._eligible(_text(event), env):
            return json.dumps({"ok": False, "reason": "没有明确的拍摄请求或条件尚未确认，请继续正常聊天"}, ensure_ascii=False)
        if _extra(event, "conditions_recorded") or env.get("changed_request"):
            return json.dumps({"ok": False, "reason": "要求改变或正在询问条件，本轮不能拍摄，请先重新确认条件"}, ensure_ascii=False)
        if not self._claim_photo(event):
            return json.dumps({"ok": False, "reason": "本轮已经执行过拍摄，请不要重复调用"}, ensure_ascii=False)
        patch = {k: v for k, v in {"outfit": outfit, "pose": pose, "expression": expression, "scene": scene}.items() if v}
        try:
            reference = _extra(event, "reference_asset", "") or (env["session"].get("last_image") if use_last_image else "")
            if _extra(event, "reference_error"):
                raise ValueError(_extra(event, "reference_error"))
            if mode not in {"persona", "scene", "edit"} or not prompt.strip():
                raise ValueError("拍摄模式或描述无效")
            job = self.storage.create_job({"status": "queued", "source": "chat", "caption": caption, "raw": _text(event), "mode": mode, "delivery_key": _extra(event, "delivery_key", ""), "umo": env["umo"], "session_key": env["key"], "persona_id": env["persona"]["id"]})
            self._photo_note(job)
            async def run():
                try:
                    generated = await self._generate_job(env, prompt, mode, patch, caption, _text(event), user_key=f"{event.get_platform_id()}:{event.get_sender_id()}", is_admin=_admin(event), reference_asset=reference, source="chat", job_id=job["id"])
                    await self._send_job(generated, event=event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    current = self.storage.job(job["id"]) or {}
                    if current.get("status") not in {"failed", "uncertain", "cancelled"}:
                        self.storage.update_job(job["id"], {"status": "failed", "error": self._error(exc)})
                    self._photo_note(self.storage.job(job["id"]))
                    if current.get("delivery_key") and current.get("status") != "uncertain":
                        self.storage.update_delivery(current["delivery_key"], {"status": "failed", "error": self._error(exc)})
                    try:
                        message = "照片已生成，但发送结果不确定，请先检查聊天记录。" if current.get("status") == "uncertain" else "这次照片没能完成：" + self._error(exc)
                        await event.send(MessageChain().message(message))
                    except Exception:
                        pass
            self._spawn(run(), "persona-canvas-chat-photo", job["id"])
            return json.dumps({"ok": True, "job_id": job["id"], "status": "queued", "image_sent": False, "description": prompt, "instruction": "角色已同意，照片正在生成，完成后插件会发图。可以简短说正在拍；不能声称已完成，本轮不要重复调用。"}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "reason": self._error(exc), "instruction": "如实说明失败，不能声称已拍好；本轮不要重复调用。"}, ensure_ascii=False)

    async def _generate_job(self, env, prompt, mode="persona", patch=None, caption="", raw="", *, user_key="webui", is_admin=False, reference_asset="", provider_name="", options=None, source="webui", job_id=""):
        self._track_current()
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
        if agreed:
            positive += "\nMandatory agreed shooting conditions: " + json.dumps(agreed, ensure_ascii=False)
        self.moderation.check_content(raw + "\n" + positive)
        provider = self._provider(provider_name)
        if source in {"active", "morning"} and not getattr(provider.capabilities, "automated_generation", True):
            raise ValueError("此生图接口要求人工发起生成，不能用于定时主动照片；请选择支持自动生成的接口")
        reference_names = env.get("approved_reference_assets") or ([reference_asset] if reference_asset else (list(dict.fromkeys(([persona["reference_asset"]] if persona.get("reference_asset") else []) + persona.get("reference_assets", []))) if persona.get("reference_enabled") and mode == "persona" else []))
        reference_limit = int(getattr(provider.capabilities, "max_reference_images", 1))
        reference_warning = f"接口单次支持 {reference_limit} 张参考图，使用列表中前 {reference_limit} 张" if len(reference_names) > reference_limit else ""
        reference_names = reference_names[:reference_limit]
        reference_name = reference_names[0] if reference_names else ""
        if mode == "edit" and not reference_name:
            raise ValueError("图片编辑需要上传参考图或选择上一张照片")
        reference_data = [self.storage.asset(name).read_bytes() for name in reference_names]
        reference = reference_data if len(reference_data) > 1 else reference_data[0] if reference_data else None
        if reference and not provider.capabilities.image_to_image:
            raise ValueError("当前接口不支持参考图，请选择支持图像输入的接口")
        options = {**copy.deepcopy(self.storage.settings["generation"]), **(options or {})}
        job_data = {"status": "queued", "source": source, "mode": mode, "umo": env["umo"], "session_key": env["key"], "persona_id": persona["id"], "persona_snapshot": persona, "session_revision": env["session"].get("revision", 0), "prompt": positive, "negative_prompt": negative, "request_prompt": prompt, "raw": raw, "caption": caption, "state_patch": patch, "provider": provider.name, "reference_asset": reference_name, "reference_assets": reference_names, "reference_warning": reference_warning, "requirements": agreed, "options": options}
        job = self.storage.update_job(job_id, job_data) if job_id else self.storage.create_job(job_data)
        self._bind_job_task(job["id"], asyncio.current_task())
        self._trace_job(job["id"], "request", detail="明确请求 / 主动机会，角色已同意")
        self._trace_job(job["id"], "role", "photo" if mode == "persona" else mode, caption)
        self._trace_job(job["id"], "conditions", detail=json.dumps(agreed, ensure_ascii=False) if agreed else "未设置额外拍摄条件")
        self._trace_job(job["id"], "reference", "warning" if reference_warning else "ok", reference_warning or f"使用 {len(reference_names)} 张真实参考图")
        self._photo_note(self.storage.job(job["id"]))
        success = False
        reserved = False
        try:
            async with self._generation_slot():
                allowed, reason = self.moderation.allow(user_key, raw + "\n" + positive, is_admin=is_admin)
                if not allowed:
                    raise ValueError(reason)
                reserved = True
                self._trace_job(job["id"], "quota", detail="生成额度已预留")
                if self.storage.job(job["id"]).get("cancel_requested"):
                    raise asyncio.CancelledError()
                if source in {"active", "morning"}:
                    budget = self.storage.reserve_budget("photos", self.storage.settings["proactive_budget"])
                    if not budget:
                        raise ValueError("今日主动照片预算已用完")
                    self.storage.finish_budget(budget, used=True)
                generating = self.storage.update_job(job["id"], {"status": "generating"})
                self._photo_note(generating)
                self._trace_job(job["id"], "provider", "running", provider.name)
                result = await asyncio.wait_for(provider.generate(positive, negative, reference=reference, options=options), timeout=max(5, min(600, int(options.get("timeout_sec", 180)))))
                if self.storage.job(job["id"]).get("cancel_requested"):
                    raise asyncio.CancelledError()
                asset = self.storage.save_asset(result.data, result.extension)
                success = True
                async with self._lock(env["key"]):
                    session = self.storage.session(env["key"], persona["id"])
                    state_committed = session.get("revision", 0) == env["session"].get("revision", 0)
                    if state_committed:
                        if mode == "persona":
                            apply_state(session, patch, self.storage.settings)
                        session["last_image"] = asset.name
                        session["pending"] = {}
                        session = self.storage.save_session(env["key"], session)
                job = self.storage.update_job(job["id"], {"status": "succeeded", "asset": asset.name, "model": result.model, "state_after": session["state"], "state_committed": state_committed, "elapsed_ms": int((time.time() - job.get("at", time.time())) * 1000)})
                job = self._trace_job(job["id"], "generation", detail="图片已验证并保存；" + ("状态已提交" if state_committed else "较新状态保留，未覆盖"))
                self._photo_note(job)
                self.storage.append_history({"ok": True, "job_id": job["id"], "image": "/assets/" + asset.name, "caption": caption, "prompt": positive, "provider": provider.name, "model": result.model, "mode": mode})
                self.storage.cleanup_history(int(options.get("max_history", 100)))
                return job
        except asyncio.CancelledError:
            cancelled = self.storage.update_job(job["id"], {"status": "cancelled", "error": "用户已撤回本次拍摄" if self.storage.job(job["id"]).get("cancel_requested") else "生成任务被停止"})
            self._photo_note(cancelled)
            raise
        except Exception as exc:
            error = self._error(exc)
            self.storage.update_job(job["id"], {"status": "failed", "error": error})
            failed = self._trace_job(job["id"], "generation", "failed", error)
            self._photo_note(failed)
            self.storage.append_history({"ok": False, "job_id": job["id"], "caption": caption or raw, "provider": provider.name, "mode": mode, "error": error})
            raise
        finally:
            if reserved:
                self.moderation.finish(user_key, success=success)

    async def _send_job(self, job, *, event=None, umo=""):
        self._track_current()
        current = self.storage.job(job["id"])
        if current.get("cancel_requested") or current.get("status") == "cancelled":
            raise asyncio.CancelledError()
        chain = MessageChain()
        if job.get("caption"):
            chain.message(job["caption"] + "\n")
        chain.file_image(str(self.storage.asset(job["asset"])))
        self.storage.update_job(job["id"], {"status": "sending"})
        if job.get("delivery_key"):
            self.storage.update_delivery(job["delivery_key"], {"status": "sending", "job_id": job["id"]})
        try:
            if event:
                accepted = await event.send(chain)
                if accepted is False:
                    raise ValueError("平台没有接受图片消息")
            else:
                result = await self.context.send_message(umo, chain)
                if result is False:
                    raise ValueError("平台没有接受主动消息")
            sent = self.storage.update_job(job["id"], {"status": "sent"})
            sent = self._trace_job(job["id"], "delivery", detail="平台已接受图片消息")
            self._photo_note(sent)
            if job.get("delivery_key"):
                self.storage.update_delivery(job["delivery_key"], {"status": "sent", "job_id": job["id"]})
        except asyncio.CancelledError:
            self.storage.update_job(job["id"], {"status": "uncertain", "error": "发送被中断，结果不确定，请先检查聊天记录"})
            self._photo_note(self._trace_job(job["id"], "delivery", "uncertain", "发送被中断"))
            if job.get("delivery_key"):
                self.storage.update_delivery(job["delivery_key"], {"status": "uncertain"})
            raise
        except Exception as exc:
            self.storage.update_job(job["id"], {"status": "uncertain", "error": "图片已生成，发送结果不确定：" + self._error(exc)})
            self._photo_note(self._trace_job(job["id"], "delivery", "uncertain", self._error(exc)))
            if job.get("delivery_key"):
                self.storage.update_delivery(job["delivery_key"], {"status": "uncertain", "error": self._error(exc)})
            raise

    async def _apply_decision(self, env, decision, text, *, event=None, explicit=False, body=None):
        if self._closing:
            raise RuntimeError("插件正在停止")
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
            reference = await self._capture_reference(event) if event else ""
            if mode == "edit" and not reference:
                reference = env["session"].get("last_image", "")
            job = await self._generate_job(env, decision["prompt"], mode, decision["state_patch"], decision["reply"], text, user_key=f"{event.get_platform_id()}:{event.get_sender_id()}", is_admin=_admin(event), reference_asset=reference, source="chat")
            if _extra(event, "delivery_key"):
                job = self.storage.update_job(job["id"], {"delivery_key": _extra(event, "delivery_key")})
            await self._send_job(job, event=event)
            await self.dialogue.remember(env, text, decision["reply"] + " [已发送照片：" + decision["prompt"] + "]")
            return {**decision, "sent": True, "job_id": job["id"]}
        session = self.storage.session(env["key"], env["persona"]["id"])
        if action == "ask":
            session["pending"] = pending_request(env, text, decision["reply"], {**infer_requirements(decision["reply"]), **state_patch(decision.get("state_patch")), **requirements(decision.get("requirements"))}, kind="photo" if explicit or self._eligible(text, env) else "state", ttl=int(self.storage.settings["dialogue"].get("confirmation_ttl_sec", 1800)))
        elif action == "state" and (explicit or state_request(text, session.get("pending"))):
            agreed = state_patch((session.get("pending") or {}).get("requirements")) if is_confirmation(text) else {}
            apply_state(session, {**decision["state_patch"], **agreed}, self.storage.settings)
            session["pending"] = {}
        elif action == "refuse":
            session["pending"] = {}
        self.storage.save_session(env["key"], session)
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

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def observe_private(self, event: AstrMessageEvent):
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
        result, elapsed = await provider.test_generation(str(body.get("prompt") or "simple blue flower on white background"))
        asset = self.storage.save_asset(result.data, result.extension)
        return {"ok": True, "provider": provider.name, "model": result.model, "elapsed_ms": elapsed, "image": image_data_url(self, asset.name)}

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
        return {"items": copy.deepcopy(self.storage.targets["items"])}

    async def web_sessions(self):
        return {"items": self.storage.list_sessions()}

    async def web_jobs(self):
        return {"items": self._jobs()}

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
        allowed = {"kind", "endpoint", "model", "api_key", "auth_header", "auth_prefix", "supports_image_edit", "supports_seed", "negative_prompt", "extra_body", "response_path", "timeout", "allow_image_urls", "allowed_image_hosts", "generation_path", "edit_path", "models_path", "connection_path", "connection_method", "prompt_field", "model_field", "negative_prompt_field", "reference_field", "reference_format", "reference_mime_field", "option_fields", "models_response_path", "supports_sampler", "supported_sizes", "params_version", "strength", "noise"}
        patch = {k: v for k, v in body.items() if k in allowed and (k != "api_key" or v)}
        if patch.get("kind", "openai") not in {"openai", "gemini", "novelai", "custom"}:
            raise ValueError("接口类型无效")
        if "extra_body" in patch and not isinstance(patch["extra_body"], dict):
            raise ValueError("额外参数必须为 JSON 对象")
        old = self.storage.settings["providers"].get(name, {})
        self.storage.settings["providers"][name] = {**old, **patch, "name": name}
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
        self.storage.save_settings()
        return await self.web_providers()

    async def web_save_settings(self, body):
        ranges = {"daily_limit": (0, 10000), "min_interval_sec": (0, 86400), "max_concurrency": (1, 16), "width": (64, 4096), "height": (64, 4096), "steps": (1, 150), "scale": (0, 30), "seed": (-1, 4294967295), "max_history": (1, 5000), "timeout_sec": (5, 300), "context_turns": (1, 100), "confirmation_ttl_sec": (30, 86400), "check_interval_sec": (15, 3600), "min_gap_sec": (60, 604800), "min_idle_sec": (0, 604800), "silence_after": (1, 100), "silence_hours": (1, 720)}
        ranges.update({"outfit_sec": (0, 2592000), "pose_sec": (0, 604800), "expression_sec": (0, 604800), "scene_sec": (0, 2592000), "messages": (0, 10000), "photos": (0, 10000), "llm": (0, 10000), "jitter_percent": (0, 40)})
        allowed_groups = {"integration", "dialogue", "llm", "moderation", "generation", "active", "good_morning", "state_lifetimes", "proactive_budget"}
        candidate = copy.deepcopy(self.storage.settings)
        for group, patch in body.items():
            if group not in allowed_groups or not isinstance(patch, dict):
                continue
            for key, value in patch.items():
                if key not in candidate[group]:
                    continue
                if key in ranges:
                    low, high = ranges[key]
                    value = float(value) if key == "scale" else int(value)
                    if not low <= value <= high:
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

    async def _queue_job(self, env, decision, text, body):
        mode = {"photo": "persona", "scene": "scene", "edit": "edit"}[decision["decision"]]
        requested_mode = str(body.get("mode") or "persona")
        if requested_mode == "scene" and mode == "persona":
            raise ValueError("角色决策与场景模式不一致，请明确要求普通场景绘图")
        options = {k: body[k] for k in ("width", "height", "steps", "scale", "seed", "sampler") if body.get(k) is not None}
        options["_explicit"] = [key for key in body.get("_explicit", list(options)) if key in options]
        job = self.storage.create_job({"status": "queued", "source": "webui", "raw": text, "caption": decision["reply"], "mode": mode, "umo": env["umo"], "session_key": env["key"], "persona_id": env["persona"]["id"]})
        self._photo_note(job)
        async def run():
            try:
                await self._generate_job(env, decision["prompt"], mode, decision["state_patch"], decision["reply"], text, reference_asset=str(body.get("reference_asset") or (env["session"].get("last_image") if mode == "edit" else "") or ""), provider_name=str(body.get("provider") or ""), options=options, source="webui", is_admin=True, job_id=job["id"])
            except Exception as exc:
                self.storage.update_job(job["id"], {"status": "failed", "error": self._error(exc)})
                self._photo_note(self._trace_job(job["id"], "generation", "failed", self._error(exc)))
        self._spawn(run(), "persona-canvas-web-job", job["id"])
        return {"job_id": job["id"], "status": "queued", "decision": decision, "caption": decision["reply"]}

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
        old = self.storage.job(str(body.get("id") or ""))
        if not old or old.get("status") not in {"failed", "cancelled"} or not old.get("persona_snapshot"):
            raise ValueError("仅能重试已失败的生成任务；发送结果不确定时请先检查聊天记录")
        if old.get("cancel_requested"):
            raise ValueError("拍摄意愿已撤回，请在生图工作台重新提交，由角色重新判断")
        persona = copy.deepcopy(old["persona_snapshot"])
        session = self.storage.session(old["session_key"], persona["id"])
        env = {"umo": old["umo"], "key": old["session_key"], "persona": persona, "session": session, "approved_requirements": old.get("requirements", {}), "approved_reference_assets": old.get("reference_assets", [])}
        decision = {"decision": {"persona": "photo", "scene": "scene", "edit": "edit"}[old["mode"]], "prompt": old["request_prompt"], "reply": old["caption"], "state_patch": old["state_patch"]}
        result = await self._queue_job(env, decision, old["raw"], {**old["options"], "reference_asset": old["reference_asset"], "provider": old["provider"], "mode": old["mode"]})
        self.storage.update_job(result["job_id"], {"retry_of": old["id"]})
        return result

    async def web_cancel_job(self, body):
        return await self._cancel_job(str(body.get("id") or ""))

    async def web_diagnostics(self):
        settings = self.storage.settings
        selected = str(settings["llm"].get("provider_id") or "")
        default = settings["providers"].get(settings["default_provider"], {})
        steps = [
            {"name": "聊天模型", "ok": (self.context.get_provider_by_id(selected) is not None if selected else settings["llm"].get("fallback_to_current", True)), "message": "沿用会话聊天模型，使用测试 LLM 验证" if not selected else selected + ("；不存在时允许回退" if settings["llm"].get("fallback_to_current", True) else "；禁用回退"), "page": "providers"},
            {"name": "绘画接口", "ok": bool(default.get("endpoint") and default.get("model") and not self._provider_status()[1]), "message": "已配置地址和模型；连接与测试图需主动验证", "page": "providers"},
            {"name": "角色外观", "ok": bool(self.storage.persona().get("positive_prompt") or self.storage.persona().get("reference_asset")), "message": "填写固定外观或上传角色参考图", "page": "persona"},
            {"name": "工具启用", "ok": None, "message": "原生模式需在 AstrBot 允许 state、photo、conditions 三个工具；兼容模式不需要工具调用", "page": "settings"},
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
            raise ValueError("目标会话不存在，请先在私聊中发送一条消息")
        if action == "remove":
            self.storage.targets["items"].remove(target)
        elif action == "toggle":
            target["enabled"] = not target.get("enabled", False)
        elif action == "test":
            accepted = await self.context.send_message(umo, MessageChain().message("这是随想画卷的连接测试消息。"))
            if accepted is False:
                raise ValueError("平台没有接受测试消息")
        elif action == "save":
            patch = body.get("patch") or body
            allowed = {"enabled", "persona_id", "start", "end", "timezone", "min_gap_sec", "min_idle_sec", "silence_after", "with_image", "morning_enabled", "morning_start", "morning_end"}
            candidate = copy.deepcopy(target)
            for key, value in patch.items():
                if key in allowed:
                    if key in {"min_gap_sec", "min_idle_sec", "silence_after"}:
                        value = int(value)
                        if value < (1 if key == "silence_after" else 0):
                            raise ValueError("间隔和未回复次数不能为负数")
                    elif key in {"start", "end", "morning_start", "morning_end"} and value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value)):
                        raise ValueError("时间格式应为 HH:MM")
                    elif key in {"enabled", "with_image", "morning_enabled"} and not isinstance(value, bool):
                        raise ValueError("目标开关必须为布尔值")
                    elif key == "timezone" and value:
                        from zoneinfo import ZoneInfo
                        ZoneInfo(str(value))
                    elif key == "persona_id" and value and not any(p["id"] == value for p in self.storage.personas["items"]):
                        raise ValueError("绑定的角色不存在")
                    candidate[key] = value
            target.clear()
            target.update(candidate)
        else:
            raise ValueError("目标操作无效")
        self.storage.save_targets()
        return await self.web_targets()

    async def web_export(self):
        return self.storage.export(include_secrets=False)
