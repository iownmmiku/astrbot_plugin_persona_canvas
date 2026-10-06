from __future__ import annotations

import asyncio
import base64
import copy
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from .active import ActiveScheduler
from .intent import Intent, heuristic_intent, parse_intent, prompt_bundle
from .moderation import Moderation
from .providers.base import ProviderError, provider_from_config
from .storage import Storage
from .webui_server import WebUI

PLUGIN_NAME = "astrbot_plugin_persona_studio"


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


@register(PLUGIN_NAME, "you", "人设驱动生图与主动消息工作室", "0.1.0")
class PersonaStudioPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.storage = Storage()
        self.moderation = Moderation(self.storage.settings)
        self.webui: WebUI | None = None
        self._tasks: set[asyncio.Task] = set()
        self._generation_lock = asyncio.Lock()
        self.active: ActiveScheduler | None = None

    def _spawn(self, coroutine, name: str) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def initialize(self):
        if bool(self.config.get("enable_webui", True)):
            token = self.storage.webui_token(str(self.config.get("webui_token", "")))
            self.webui = WebUI(self, host=str(self.config.get("webui_host", "127.0.0.1")), port=int(self.config.get("webui_port", 3018)), token=token)
            try:
                self.webui.start()
                logger.info("[人设影像] WebUI 已启动：%s", self.webui.url)
            except Exception as exc:
                logger.error("[人设影像] WebUI 启动失败：%s", exc)
                self.webui = None
        self.active = ActiveScheduler(self)
        if self.storage.settings.get("active", {}).get("enabled") or self.storage.settings.get("good_morning", {}).get("enabled"):
            self._spawn(self.active.run(), "persona-studio-active")

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

    async def _text_provider(self):
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

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
        return event.chain_result([MessageChain().message(caption), MessageChain().file_image(str(path))])

    @filter.command("生图控制台", alias={"人设控制台"})
    async def command_webui(self, event: AstrMessageEvent):
        if not _is_admin(event):
            yield event.plain_result("只有管理员可以打开人设影像控制台。")
            return
        if not self.webui:
            yield event.plain_result("WebUI 未启动，请在插件配置中开启后重载插件。")
            return
        yield event.plain_result(f"人设影像控制台：{self.webui.url}\n访问令牌：{self.webui.token}")

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
            yield event.image_result(str(self.storage.save_asset(data, ext)))
        except Exception as exc:
            yield event.plain_result(f"生图失败：{exc}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def natural_route(self, event: AstrMessageEvent):
        if not bool(self.config.get("auto_route", True)):
            return
        text = _text_from_event(event)
        if not text or text.startswith("/"):
            return
        provider = await self._text_provider()
        intent = await parse_intent(provider, text, False)
        if not intent.is_generation:
            return
        try:
            data, ext, _ = await self._generate(intent, user_key=event.unified_msg_origin, is_admin=_is_admin(event))
            await event.send(event.chain_result([MessageChain().message("生成完成"), MessageChain().file_image(str(self.storage.save_asset(data, ext)))]))
        except Exception as exc:
            await event.send(event.plain_result(f"生图失败：{exc}"))

    @filter.platform_adapter_type(
        filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.QQOFFICIAL
    )
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def observe_private(self, event: AstrMessageEvent):
        if self.active:
            self.active.observe(event)

    # ---------- WebUI API ----------
    async def web_state(self):
        return {"persona": copy.deepcopy(self.storage.persona()), "settings": copy.deepcopy(self.storage.settings), "targets": copy.deepcopy(self.storage.targets.get("items", [])), "history": self.storage.recent_history(100)}

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
        return {"items": self.storage.recent_history(100)}

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
                result[key] = PersonaStudioPlugin._merge_settings(result[key], value)
            else:
                result[key] = value
        return result

    async def web_generate(self, body: dict):
        text = str(body.get("text") or body.get("prompt") or "").strip()
        intent = heuristic_intent(text, False)
        if body.get("mode") == "scene":
            intent = Intent(mode="scene", scene_prompt=text, raw=text)
        elif body.get("mode") == "persona":
            intent = Intent(mode="persona_edit", use_persona=True, prompt_delta=text, raw=text)
        data, ext, caption = await self._generate(intent, user_key="webui", is_admin=True)
        asset = self.storage.save_asset(data, ext)
        return {"image": f"/assets/{asset.name}", "caption": caption}

    async def web_target_action(self, body: dict):
        action, umo = str(body.get("action", "")), str(body.get("umo", ""))
        items = self.storage.targets.setdefault("items", [])
        target = next((item for item in items if item.get("umo") == umo), None)
        if action == "remove":
            self.storage.targets["items"] = [item for item in items if item.get("umo") != umo]
        elif action == "toggle" and target:
            target["enabled"] = not bool(target.get("enabled", False))
        elif action == "test" and target:
            await self.context.send_message(umo, MessageChain().message("这是人设影像工作室的测试消息。"))
        self.storage.save_targets()
        return {"items": copy.deepcopy(self.storage.targets.get("items", []))}
