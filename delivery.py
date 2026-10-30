"""Shared, read-only checks before committing or delivering a role action."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime


async def delivery_guard(plugin, *, env=None, job=None):
    snapshot = job or {}
    umo = str(snapshot.get("umo") or (env or {}).get("umo") or "")
    key = str(snapshot.get("session_key") or (env or {}).get("key") or "")
    persona_id = snapshot.get("persona_id") or (env or {}).get("persona", {}).get("id")
    identity = snapshot.get("identity") or ({name: env.get(name, "") for name in ("astro_id", "cid")} if env and "astro_id" in env else None)
    source = snapshot.get("source") or (env or {}).get("delivery_source")
    if identity is not None and umo and not umo.startswith("webui:"):
        try:
            current_identity = await asyncio.wait_for(plugin.dialogue.current_identity(umo), 15)
        except asyncio.CancelledError:
            raise
        except Exception:
            return "当前 AstrBot 人设或会话暂不可用，保留结果，不发送"
        if any(str(current_identity.get(name) or "") != str(identity.get(name) or "") for name in ("astro_id", "cid")):
            return "AstrBot 人设或会话已改变，旧内容不再发送"

    return local_delivery_guard(plugin, env=env, job=job)


def local_delivery_guard(plugin, *, env=None, job=None):
    """No await or persistence; safe to repeat after another provider yields."""
    snapshot = job or {}
    umo = str(snapshot.get("umo") or (env or {}).get("umo") or "")
    key = str(snapshot.get("session_key") or (env or {}).get("key") or "")
    persona_id = snapshot.get("persona_id") or (env or {}).get("persona", {}).get("id")
    identity = snapshot.get("identity") or ({name: env.get(name, "") for name in ("astro_id", "cid")} if env and "astro_id" in env else None)
    source = snapshot.get("source") or (env or {}).get("delivery_source")
    # All mutable local checks follow the final asynchronous identity lookup.
    if plugin._closing:
        return "插件正在停止"
    if not umo.startswith("webui:") and not plugin.storage.settings["integration"].get("enabled", True):
        return "聊天接入已停用，旧内容不再发送"
    current_job = plugin.storage.job(snapshot["id"]) if snapshot.get("id") else snapshot
    current_job = current_job or {}
    if current_job.get("cancel_requested") or current_job.get("status") == "cancelled":
        return "拍摄已撤回，不再发送"
    if current_job.get("recovered"):
        return "取回的历史工作流结果仅供查看，不自动发送"
    target = next((item for item in plugin.storage.targets["items"] if item.get("umo") == umo), None)
    if identity is not None and source != "webui" and not umo.startswith("webui:"):
        bound = next((item for item in plugin.storage.personas["items"] if item.get("astrbot_persona_id") == identity.get("astro_id")), None)
        selected = (target or {}).get("persona_id") or (bound or {}).get("id") or plugin.storage.settings.get("current_persona", "default")
        if selected != persona_id:
            return "当前绑定的视觉角色已改变，旧内容不再发送"
    session = plugin.storage.read_session(key) if key else None
    if key and (not session or session.get("persona_id") != persona_id):
        return "视觉角色或会话已改变，旧内容不再发送"
    expected_revision = current_job.get("delivery_revision") if job else (env or {}).get("session", {}).get("revision")
    if session and expected_revision is not None and session.get("revision", 0) != expected_revision:
        return "视觉状态或拍摄条件已改变，旧内容不再发送"
    pending = (session or {}).get("pending") or {}
    expected_pending = current_job.get("delivery_pending_id") if job else ((env or {}).get("session", {}).get("pending") or {}).get("request_id", "")
    if expected_pending is not None and pending.get("request_id", "") != expected_pending:
        return "拍摄条件已替换或撤回，旧内容不再发送"
    if source in {"active", "morning", "reply_image"}:
        if not target:
            return "聊天目标已移除，旧内容不再发送"
        if source == "reply_image":
            if not target.get("reply_image_enabled") or not plugin.storage.settings["reply_images"].get("enabled"):
                return "聊天配图已停用"
        else:
            config = plugin.storage.settings["good_morning" if source == "morning" else "active"]
            if target.get("scope") == "group" or not target.get("enabled") or not config.get("enabled"):
                return "主动联系已停用"
            if source == "morning" and not target.get("morning_enabled", True):
                return "此聊天的早安已停用"
            if job and (not target.get("with_image", True) or (source == "morning" and config.get("image_mode") == "text_only")):
                return "当前目标不允许主动图片"
            if job and current_job.get("delivery_key"):
                from .active import ActiveScheduler
                scheduler = plugin.active or ActiveScheduler(plugin)
                delivery = plugin.storage.delivery(current_job["delivery_key"]) or {}
                # Earlier phases have dedicated scheduler checks and diagnostics.
                # This closes only the final identity-await gap before sending.
                if delivery.get("status") != "sending":
                    return ""
                if target.get("last_inbound", 0) != delivery.get("inbound_at", 0):
                    return "用户已发来新消息，旧主动图片不再发送"
                if scheduler._base_rules(target, time.time())[0] or scheduler._window_rules(target, source):
                    return "主动联系的空闲、静默或时间窗已改变"
                initial = delivery.get("proactive_context", {}).get("time")
                if initial:
                    clock = scheduler._time_context(target, source)
                    elapsed = (datetime.fromisoformat(clock["now"]) - datetime.fromisoformat(initial["now"])).total_seconds()
                    if not all(clock[name] == initial[name] for name in ("timezone", "date", "period")) or not 0 <= elapsed <= 900:
                        return "主动内容的时间依据已过期，保留原图"
                    from .temporal import temporal_conflicts
                    if temporal_conflicts(current_job.get("caption", ""), clock, morning=source == "morning"):
                        return "主动图片的文字时间已过期，保留原图"
    return ""
