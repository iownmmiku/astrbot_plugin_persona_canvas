from __future__ import annotations

import asyncio
import base64
import copy
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Purge cached submodules on reload so new Python code on disk is always used
_pkg = __name__.rpartition(".")[0]
if _pkg:
    for _m in list(sys.modules.keys()):
        if _m.startswith(_pkg + ".") and _m != __name__:
            del sys.modules[_m]

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from .active import ActiveScheduler
from .intent import Intent, heuristic_intent, parse_intent, prompt_bundle
from .moderation import Moderation
from .page_api import CanvasPageApi, image_data_url
from .providers.base import ProviderError, provider_from_config
from .storage import Storage
from .webui_server import WebUI

PLUGIN_NAME = "astrbot_plugin_persona_canvas"


def _text_from_event(event: AstrMessageEvent) -> str:
    try:
        return str(event.message_str or "").strip()
    except Exception:
        return ""


def _is_admin(event: AstrMessageEvent) -> bool:
    try:
        return bool(event.is_admin())
    except Exception:
        return False


@register(PLUGIN_NAME, "you", "随想画卷（Persona Canvas）：人设驱动生图与主动消息", "0.4.6")
class PersonaCanvasPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.storage = Storage()
        self._apply_plugin_config()
        self.moderation = Moderation(self.storage.settings)
        self.webui: WebUI | None = None
        self._tasks: set[asyncio.Task] = set()
        self._generation_lock = asyncio.Lock()
        self.active: ActiveScheduler | None = None
        self.page_api = CanvasPageApi(self)

    def _apply_plugin_config(self) -> None:
        """Merge the AstrBot config form into persisted runtime defaults."""
        cfg = self.config
        settings = self.storage.settings
        settings["moderation"] = self._merge_settings(settings.get("moderation", {}), {
            "enabled": bool(cfg.get("moderation_enabled", True)),
            "daily_limit": max(0, int(cfg.get("moderation_daily_limit", 5))),
            "min_interval_sec": max(0, int(cfg.get("moderation_min_interval_sec", 20))),
            "max_concurrency": max(1, int(cfg.get("moderation_max_concurrency", 1))),
        })
        settings["generation"] = self._merge_settings(settings.get("generation", {}), {
            "width": max(64, int(cfg.get("generation_width", 832))),
            "height": max(64, int(cfg.get("generation_height", 1216))),
            "steps": max(1, int(cfg.get("generation_steps", 28))),
            "scale": max(0.0, float(cfg.get("generation_scale", 5.0))),
            "sampler": str(cfg.get("generation_sampler", "k_euler_ancestral")),
            "seed": int(cfg.get("generation_seed", -1)),
            "max_history": max(1, int(cfg.get("generation_max_history", 100))),
        })
        settings["active"] = self._merge_settings(settings.get("active", {}), {
            "enabled": bool(cfg.get("active_enabled", False)),
            "check_interval_sec": max(15, int(cfg.get("active_check_interval_sec", 30))),
            "default_start": str(cfg.get("active_start", "09:00")),
            "default_end": str(cfg.get("active_end", "22:00")),
            "min_gap_sec": max(60, int(cfg.get("active_min_gap_sec", 3600))),
            "silence_after": max(1, int(cfg.get("active_silence_after", 3))),
            "silence_hours": max(1, int(cfg.get("active_silence_hours", 24))),
        })
        settings["good_morning"] = self._merge_settings(settings.get("good_morning", {}), {
            "enabled": bool(cfg.get("good_morning_enabled", False)),
            "start": str(cfg.get("good_morning_start", "07:00")),
            "end": str(cfg.get("good_morning_end", "10:00")),
            "timezone": str(cfg.get("good_morning_timezone", "Asia/Shanghai")),
        })
        self.storage.save_settings()

    def _spawn(self, coroutine, name: str) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def initialize(self):
        self.page_api.register_routes()
        if bool(self.config.get("enable_webui", True)):
            token = self.storage.webui_token(str(self.config.get("webui_token", "")))
            self.webui = WebUI(self, host=str(self.config.get("webui_host", "127.0.0.1")), port=int(self.config.get("webui_port", 3018)), token=token)
            try:
                self.webui.start()
                logger.info("[随想画卷] WebUI 已启动：%s", self.webui.url)
            except Exception as exc:
                logger.error("[随想画卷] WebUI 启动失败：%s", exc)
                self.webui = None
        self.active = ActiveScheduler(self)
        if self.storage.settings.get("active", {}).get("enabled") or self.storage.settings.get("good_morning", {}).get("enabled"):
            self._spawn(self.active.run(), "persona-canvas-active")

    async def terminate(self):
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.webui:
            await self.webui.close()
            self.webui = None

    def _provider(self, name: str | None = None):
        providers = self.storage.settings.get("providers", {})
        key = name or self.storage.settings.get("default_provider", "default")
        config = providers.get(key) or providers.get("default")
        if not isinstance(config, dict):
            raise ProviderError("没有配置可用的生图接口")
        return provider_from_config(key, config)

    async def _text_provider(self, umo: str | None = None):
        llm = self.storage.settings.get("llm", {})
        provider_id = str(llm.get("provider_id") or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider is None:
                raise ProviderError(f"找不到已配置的 LLM Provider：{provider_id}")
            return provider
        if bool(llm.get("fallback_to_current", True)):
            provider = await self.context.get_using_provider_async(umo)
            if provider is not None:
                return provider
        return None

    async def _llm_chat(self, prompt: str, system_prompt: str = "", contexts: list | None = None, umo: str | None = None) -> str:
        provider = await self._text_provider(umo)
        if provider is None:
            raise ProviderError("没有可用的 LLM Provider，请在随想画卷页面选择模型")
        llm = self.storage.settings.get("llm", {})
        kwargs = {"prompt": prompt, "contexts": contexts or [], "system_prompt": system_prompt}
        if str(llm.get("model") or "").strip():
            kwargs["model"] = str(llm["model"]).strip()
        timeout = max(5, min(300, int(llm.get("timeout_sec", 45))))
        response = await asyncio.wait_for(provider.text_chat(**kwargs), timeout=timeout)
        text = (getattr(response, "completion_text", "") or "").strip()
        if not text:
            raise ProviderError("LLM 没有返回有效文本")
        return text

    async def _llm_providers(self):
        items = []
        for provider in self.context.get_all_providers():
            meta = provider.meta()
            item = {"id": str(meta.id), "model": str(meta.model or ""), "type": str(meta.type), "provider_type": getattr(meta.provider_type, "value", str(meta.provider_type)), "models": [], "error": ""}
            try:
                item["models"] = await asyncio.wait_for(provider.get_models(), timeout=20)
            except Exception as exc:
                item["error"] = str(exc)[:300]
            items.append(item)
        return {"items": items, "selected": copy.deepcopy(self.storage.settings.get("llm", {}))}

    async def _test_llm(self, body: dict):
        provider_id = str(body.get("provider_id") or self.storage.settings.get("llm", {}).get("provider_id") or "").strip()
        provider = self.context.get_provider_by_id(provider_id) if provider_id else await self.context.get_using_provider_async(None)
        if provider is None:
            raise ProviderError("没有找到要测试的 LLM Provider")
        model = str(body.get("model") or self.storage.settings.get("llm", {}).get("model") or "").strip()
        started = time.monotonic()
        kwargs = {"prompt": "Reply with PONG only.", "contexts": [], "system_prompt": "You are a connectivity test."}
        if model:
            kwargs["model"] = model
        response = await asyncio.wait_for(provider.text_chat(**kwargs), timeout=30)
        meta = provider.meta()
        return {"ok": True, "provider_id": str(meta.id), "model": model or str(meta.model or ""), "text": (response.completion_text or "").strip()[:200], "elapsed_ms": int((time.monotonic() - started) * 1000)}

    async def _generate(self, intent: Intent, *, user_key: str, is_admin: bool = False, reference: bytes | None = None) -> tuple[bytes, str, str]:
        allowed, reason = self.moderation.allow(user_key, intent.raw or intent.scene_prompt, is_admin=is_admin)
        if not allowed:
            raise ProviderError(reason)
        try:
            async with self._generation_lock:
                persona = self.storage.persona()
                if intent.state_patch and intent.use_persona:
                    state = persona.setdefault("state", {})
                    state.update(intent.state_patch)
                    persona["updated_at"] = time.time()
                    self.storage.save_personas()
                positive, negative = prompt_bundle(persona, intent)
                provider = self._provider(intent.provider)
                options = self.storage.settings.get("generation", {})
                result = await provider.generate(positive, negative, reference=reference, options=options)
                asset = self.storage.save_asset(result.data, result.extension)
                self.storage.append_history({"ok": True, "image": f"/assets/{asset.name}", "provider": result.provider, "model": result.model, "caption": intent.caption or intent.raw, "prompt": positive, "mode": intent.mode})
                return result.data, result.extension, intent.caption or "生成完成"
        except Exception as exc:
            self.storage.append_history({"ok": False, "provider": intent.provider, "caption": intent.caption or intent.raw, "error": str(exc), "mode": intent.mode})
            raise
        finally:
            self.moderation.finish(user_key)

    async def _reply_image(self, event: AstrMessageEvent, data: bytes, extension: str, caption: str):
        path = self.storage.save_asset(data, extension)
        result = event.make_result()
        if caption and caption != "生成完成":
            result.message(caption + "\n")
        result.file_image(str(path))
        return result

    @filter.command("生图控制台", alias={"人设控制台"})
    async def command_webui(self, event: AstrMessageEvent):
        if not _is_admin(event):
            yield event.plain_result("只有管理员可以打开随想画卷控制台。")
            return
        if not self.webui:
            yield event.plain_result("WebUI 未启动，请在插件配置中开启后重载插件。")
            return
        yield event.plain_result(f"随想画卷控制台：{self.webui.url}\n访问令牌：{self.webui.token}")

    @filter.command("生图")
    async def command_generate(self, event: AstrMessageEvent, text: str = ""):
        prompt = text.strip() or _text_from_event(event).removeprefix("/生图").strip()
        if not prompt:
            yield event.plain_result("请在 /生图 后输入描述。")
            return
        intent = heuristic_intent(prompt, False)
        if not intent.is_generation:
            intent = Intent(mode="persona_selfie", use_persona=True, prompt_delta=prompt, raw=prompt)
        try:
            data, ext, caption = await self._generate(intent, user_key=event.unified_msg_origin, is_admin=_is_admin(event))
            path = self.storage.save_asset(data, ext)
            logger.info(f"[随想画卷] 正在发送图片: {path} (会话: {event.unified_msg_origin})")
            result = event.make_result()
            if caption and caption != "生成完成":
                result.message(caption + "\n")
            result.file_image(str(path))
            event.should_call_llm(True)
            yield result
        except Exception as exc:
            yield event.plain_result(f"生图失败：{exc}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def natural_route(self, event: AstrMessageEvent):
        if not bool(self.config.get("auto_route", True)):
            return
        text = _text_from_event(event)
        if not text or text.startswith("/"):
            return
        provider = await self._text_provider(event.unified_msg_origin)
        llm = self.storage.settings.get("llm", {})
        intent = await parse_intent(provider, text, False, model=str(llm.get("model") or ""), timeout=int(llm.get("timeout_sec", 45)))
        if not intent.is_generation:
            return
        try:
            data, ext, caption = await self._generate(intent, user_key=event.unified_msg_origin, is_admin=_is_admin(event))
            path = self.storage.save_asset(data, ext)
            logger.info(f"[随想画卷] 正在发送图片: {path} (会话: {event.unified_msg_origin})")
            result = event.make_result()
            if caption and caption != "生成完成":
                result.message(caption + "\n")
            result.file_image(str(path))
            event.should_call_llm(True)
            yield result
        except Exception as exc:
            yield event.plain_result(f"生图失败：{exc}")

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.QQOFFICIAL
    )
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def observe_private(self, event: AstrMessageEvent):
        if self.active:
            self.active.observe(event)

    # ---------- WebUI API ----------
    async def web_state(self):
        safe_settings = copy.deepcopy(self.storage.settings)
        providers = safe_settings.get("providers", {})
        for item in providers.values():
            if isinstance(item, dict):
                item.pop("api_key", None)
        return {"persona": copy.deepcopy(self.storage.persona()), "settings": safe_settings, "targets": copy.deepcopy(self.storage.targets.get("items", [])), "history": self._history_with_assets(100)}

    def _history_with_assets(self, limit: int = 100) -> list[dict]:
        items = self.storage.recent_history(limit)
        for item in items:
            image = str(item.get("image") or "")
            if image.startswith("/assets/"):
                item["image"] = image_data_url(self, image.rsplit("/", 1)[-1])
        return items

    async def web_personas(self):
        return {"items": copy.deepcopy(self.storage.personas.get("items", []))}

    async def web_providers(self):
        result = []
        for name, item in self.storage.settings.get("providers", {}).items():
            safe = {key: value for key, value in item.items() if key != "api_key"}
            safe["name"] = name
            result.append(safe)
        return {"items": result}

    async def web_targets(self):
        return {"items": copy.deepcopy(self.storage.targets.get("items", []))}

    async def web_history(self):
        return {"items": self._history_with_assets(100)}

    async def web_save_persona(self, body: dict):
        item = self.storage.upsert_persona(body)
        if body.get("set_current", True):
            self.storage.settings["current_persona"] = item["id"]
            self.storage.save_settings()
        return item

    async def web_save_settings(self, body: dict):
        self.storage.settings = self._merge_settings(self.storage.settings, body)
        self.storage.save_settings()
        return self.storage.settings

    async def web_save_provider(self, body: dict):
        name = str(body.get("name") or "default").strip()[:80]
        if not name:
            raise ProviderError("接口名称不能为空")
        providers = self.storage.settings.setdefault("providers", {})
        old = providers.get(name, {})
        item = {**old, **{key: value for key, value in body.items() if key != "api_key" or value}}
        if not body.get("api_key") and old.get("api_key"):
            item["api_key"] = old["api_key"]
        item["name"] = name
        providers[name] = item
        self.storage.save_settings()
        return {key: value for key, value in item.items() if key != "api_key"}

    @staticmethod
    def _merge_settings(base: dict, patch: dict) -> dict:
        result = copy.deepcopy(base)
        for key, value in patch.items():
            if isinstance(result.get(key), dict) and isinstance(value, dict):
                result[key] = PersonaCanvasPlugin._merge_settings(result[key], value)
            else:
                result[key] = value
        return result

    async def web_generate(self, body: dict):
        text = str(body.get("text") or body.get("prompt") or "").strip()
        intent = heuristic_intent(text, False)
        if body.get("mode") == "scene":
            intent = Intent(mode="scene", scene_prompt=text, raw=text, provider=str(body.get("provider") or "default"))
        elif body.get("mode") == "persona":
            intent = Intent(mode="persona_edit", use_persona=True, prompt_delta=text, raw=text, provider=str(body.get("provider") or "default"))
        options = copy.deepcopy(self.storage.settings.get("generation", {}))
        for key in ("width", "height", "steps", "scale", "seed"):
            if body.get(key) is not None:
                try:
                    options[key] = int(body[key]) if key != "scale" else float(body[key])
                except (TypeError, ValueError):
                    pass
        original = self.storage.settings.get("generation", {})
        self.storage.settings["generation"] = options
        try:
            data, ext, caption = await self._generate(intent, user_key="webui", is_admin=True)
        finally:
            self.storage.settings["generation"] = original
        asset = self.storage.save_asset(data, ext)
        return {"image": image_data_url(self, asset.name), "caption": caption}

    async def web_target_action(self, body: dict):
        action, umo = str(body.get("action", "")), str(body.get("umo", ""))
        items = self.storage.targets.setdefault("items", [])
        target = next((item for item in items if item.get("umo") == umo), None)
        if action == "remove":
            self.storage.targets["items"] = [item for item in items if item.get("umo") != umo]
        elif action == "toggle" and target:
            target["enabled"] = not bool(target.get("enabled", False))
        elif action == "test" and target:
            await self.context.send_message(umo, MessageChain().message("这是随想画卷的测试消息。"))
        self.storage.save_targets()
        return {"items": copy.deepcopy(self.storage.targets.get("items", []))}
