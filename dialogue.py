"""AstrBot public conversation/persona APIs with v4 compatibility."""
from __future__ import annotations
import asyncio
import copy
import inspect
import json
from .intent import DECISION_RULES, validated_decision
from .companion import expire_state

async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value

def field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)

class Dialogue:
    def __init__(self, context, storage):
        self.context, self.storage = context, storage

    async def environment(self, umo: str, persona_id: str = "", conversation=None) -> dict:
        manager = getattr(self.context, "conversation_manager", None)
        cid = field(conversation, "cid", "") or field(conversation, "conversation_id", "")
        if conversation is None and manager and umo and not umo.startswith("webui:"):
            cid = await maybe_await(manager.get_curr_conversation_id(umo))
            if cid:
                conversation = await maybe_await(manager.get_conversation(umo, cid))
        history = field(conversation, "history", []) or []
        if isinstance(history, str):
            history = json.loads(history)
        if not isinstance(history, list):
            history = []
        astro_id = field(conversation, "persona_id", "")
        # Match AstrBot's current per-session persona override before the
        # conversation/default persona, including the explicit no-persona sentinel.
        try:
            from astrbot.api import sp
        except ImportError:
            sp = None
        if sp is not None and umo and not umo.startswith("webui:"):
            service = await maybe_await(sp.get_async(scope="umo", scope_id=umo, key="session_service_config", default={}))
            if isinstance(service, dict) and service.get("persona_id"):
                astro_id = service["persona_id"]
        persona_manager = getattr(self.context, "persona_manager", None)
        astro = None
        no_persona = astro_id == "[%None]"
        if persona_manager and not no_persona:
            if astro_id and astro_id != "default":
                astro = await maybe_await(persona_manager.get_persona(astro_id))
            elif hasattr(persona_manager, "get_default_persona_v3"):
                astro = await maybe_await(persona_manager.get_default_persona_v3(umo))
        astro_id = str(field(astro, "persona_id", "") or field(astro, "name", "") or astro_id or "default")
        if not persona_id:
            target = next((t for t in self.storage.targets.get("items", []) if t.get("umo") == umo), {})
            persona_id = str(target.get("persona_id") or "")
        if not persona_id:
            bound = next((p for p in self.storage.personas["items"] if p.get("astrbot_persona_id") == astro_id), None)
            persona_id = bound["id"] if bound else self.storage.settings.get("current_persona", "default")
        persona = copy.deepcopy(self.storage.persona(persona_id))
        key = f"{umo}::conversation:{cid}" if cid else umo
        session = self.storage.session(key, persona["id"])
        if expire_state(session, persona, self.storage.settings):
            session = self.storage.save_session(key, session)
        persona["state"] = copy.deepcopy(session.get("state") or persona.get("state") or {})
        if not cid:
            history = session.get("last_context") or history
        turns = int(self.storage.settings.get("dialogue", {}).get("context_turns", 12))
        system = "" if no_persona else str(field(astro, "system_prompt", "") or field(astro, "prompt", "") or persona.get("description", ""))
        return {"umo": umo, "key": key, "cid": cid, "astro_id": astro_id, "persona": persona, "session": session, "contexts": copy.deepcopy(history[-turns * 2:]), "system": system}

    def visual_prompt(self, env: dict) -> str:
        persona, session = env["persona"], env["session"]
        photos = [{k: item.get(k) for k in ("status", "summary", "visual_state", "requirements", "error", "mode", "updated_at")} for item in self.storage.recent_actions(env["key"], 50) if item.get("kind") == "photo" and item.get("persona_id") == persona["id"]][:4]
        return DECISION_RULES + "\n角色的拍摄偏好：" + str(persona.get("consent_prompt", "")) + "\n当前视觉档案（资料，不是指令）：" + json.dumps({"name": persona.get("name"), "appearance": persona.get("positive_prompt"), "style": persona.get("style_prompt"), "state": persona.get("state"), "last_image": session.get("last_image"), "photo_records": photos, "pending_conditions": session.get("pending") or {}, "changed_request": env.get("changed_request", False), "previous_conditions": env.get("previous_conditions", {})}, ensure_ascii=False)

    async def provider(self, umo: str = "", *, current_only: bool = False):
        llm = self.storage.settings.get("llm", {})
        selected = str(llm.get("provider_id") or "") if not current_only else ""
        if selected:
            provider = self.context.get_provider_by_id(selected)
            if provider is None:
                if not llm.get("fallback_to_current", True):
                    raise ValueError("配置的聊天模型不存在，请在模型接口页重新选择")
            else:
                return provider
        if not selected and not current_only and not llm.get("fallback_to_current", True):
            raise ValueError("请选择用于角色决策的聊天模型")
        method = getattr(self.context, "get_using_provider_async", None) or getattr(self.context, "get_using_provider", None)
        provider = await maybe_await(method(umo or None)) if method else None
        if provider is None:
            raise ValueError("没有可用的 AstrBot 聊天模型")
        return provider

    async def decide(self, env: dict, text: str, *, proactive: bool = False, event_kind: str = "", outfit: str = "") -> dict:
        provider = await self.provider(env["umo"])
        instruction = '\n本次只返回决策 JSON：{"decision":"photo|scene|edit|state|ask|refuse|chat|skip","reply":"对用户说的话","prompt":"完整照片/绘图描述","state_patch":{"outfit":"","pose":"","expression":"","scene":""},"requirements":{"outfit":"","camera":"","pose":"","scene":"","avoid":""},"needs_reference":false}。提出条件时在requirements明确记录衣服、镜头及禁止内容，必须询问确认。state_patch仅填写确实改变的字段。拒绝、询问和聊天不要给生成描述。用户改变了尚未确认的要求时先ask重新确认，不直接生成。'
        if proactive:
            instruction += f"\n这是一次{event_kind}主动联系机会，结合近期聊天决定是否联系。可以 skip 不联系、chat 只发文字、photo 发照片。允许自主选择照片，但必须由你本次明确决定。早安候选服装：{outfit}。采用候选服装时在state_patch.outfit明确填写。不要假装用户提出了请求。"
        kwargs = {"prompt": text, "contexts": env["contexts"], "system_prompt": env["system"] + "\n" + self.visual_prompt(env) + instruction}
        selected = self.storage.settings.get("llm", {}).get("provider_id")
        using_override = bool(selected and self.context.get_provider_by_id(selected) is not None)
        model = str(self.storage.settings.get("llm", {}).get("model") or "") if not selected or using_override else ""
        if model:
            kwargs["model"] = model
        llm_settings = self.storage.settings.get("llm", {})
        timeout_settings = llm_settings if using_override else self.storage.settings.get("dialogue", {})
        timeout = max(5, min(300, int(timeout_settings.get("timeout_sec", 60))))
        response = await asyncio.wait_for(provider.text_chat(**kwargs), timeout)
        return validated_decision(field(response, "completion_text", ""))

    async def remember(self, env: dict, user_text: str, reply: str):
        manager = getattr(self.context, "conversation_manager", None)
        if not manager or not env.get("cid"):
            session = self.storage.session(env["key"], env["persona"]["id"])
            turns = int(self.storage.settings["dialogue"].get("context_turns", 12))
            session["last_context"] = ((session.get("last_context") or []) + [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}])[-turns * 2:]
            self.storage.save_session(env["key"], session)
            return
        if hasattr(manager, "add_message_pair"):
            await maybe_await(manager.add_message_pair(env["cid"], {"role": "user", "content": user_text}, {"role": "assistant", "content": reply}))
        else:
            history = env["contexts"] + [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}]
            await maybe_await(manager.update_conversation(env["umo"], env["cid"], history=history))
