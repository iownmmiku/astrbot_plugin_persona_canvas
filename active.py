from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import MessageChain


class ActiveScheduler:
    def __init__(self, plugin):
        self.plugin = plugin
        self._last_tick = 0.0
        self._seen: dict[str, dict] = {}

    def observe(self, event) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        target = next((x for x in self.plugin.storage.targets.get("items", []) if x.get("umo") == umo), None)
        if target is None:
            message = getattr(event, "message_obj", None)
            sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
            target = {"umo": umo, "sender_id": sender_id, "platform": str(getattr(event, "get_platform_id", lambda: "")() or ""), "enabled": False, "unanswered": 0, "last_inbound": time.time(), "last_active": 0, "silent_until": 0, "morning_date": ""}
            self.plugin.storage.targets.setdefault("items", []).append(target)
        target["last_inbound"] = time.time()
        target["unanswered"] = 0
        target["silent_until"] = 0
        self.plugin.storage.save_targets()

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[人设影像] 主动消息检查失败：%s", exc)
            interval = max(15, int(self.plugin.storage.settings.get("active", {}).get("check_interval_sec", 30)))
            await asyncio.sleep(interval)

    async def tick(self):
        settings = self.plugin.storage.settings
        active = settings.get("active", {})
        morning = settings.get("good_morning", {})
        now = time.time()
        for target in list(self.plugin.storage.targets.get("items", [])):
            if not target.get("enabled"):
                continue
            if active.get("enabled") and self._in_window(active.get("default_start", "09:00"), active.get("default_end", "22:00")):
                await self._ordinary(target, active, now)
            if morning.get("enabled") and self._in_window(morning.get("start", "07:00"), morning.get("end", "10:00"), morning.get("timezone", "Asia/Shanghai")):
                await self._morning(target, morning, now)

    @staticmethod
    def _in_window(start: str, end: str, timezone: str = "Asia/Shanghai") -> bool:
        try:
            now = datetime.now(ZoneInfo(timezone))
            current = now.hour * 60 + now.minute
            sh, sm = map(int, str(start).split(":")[:2])
            eh, em = map(int, str(end).split(":")[:2])
            left, right = sh * 60 + sm, eh * 60 + em
            return left <= current <= right if left <= right else current >= left or current <= right
        except Exception:
            return False

    async def _ordinary(self, target: dict, settings: dict, now: float):
        if now < float(target.get("silent_until", 0) or 0):
            return
        min_gap = max(60, int(target.get("min_gap_sec", settings.get("min_gap_sec", 3600))))
        if now - float(target.get("last_active", 0) or 0) < min_gap:
            return
        silence_after = max(1, int(target.get("silence_after", settings.get("silence_after", 3))))
        if int(target.get("unanswered", 0)) >= silence_after:
            target["silent_until"] = now + max(3600, int(settings.get("silence_hours", 24)) * 3600)
            self.plugin.storage.save_targets()
            return
        provider = await self.plugin._text_provider()
        message = "好久没聊了，最近过得怎么样？"
        if provider:
            try:
                response = await provider.text_chat(prompt="请结合近期上下文写一句自然、简短、不施压的破冰消息。只输出消息本身。", contexts=[], system_prompt=str(self.plugin.storage.persona().get("description", "")))
                message = (getattr(response, "completion_text", "") or message).strip()[:300]
            except Exception:
                pass
        await self.plugin.context.send_message(target["umo"], MessageChain().message(message))
        target["last_active"] = now
        target["unanswered"] = int(target.get("unanswered", 0)) + 1
        self.plugin.storage.save_targets()

    async def _morning(self, target: dict, settings: dict, now: float):
        timezone = str(settings.get("timezone", "Asia/Shanghai"))
        try:
            local_date = datetime.now(ZoneInfo(timezone)).date().isoformat()
        except Exception:
            local_date = datetime.now().date().isoformat()
        if target.get("morning_date") == local_date:
            return
        # Claim before generation so a restart or concurrent tick cannot double-send.
        target["morning_date"] = local_date
        self.plugin.storage.save_targets()
        try:
            persona = self.plugin.storage.persona()
            pool = persona.get("outfit_pool") or ["casual morning clothes"]
            outfit = random.choice(pool)
            from .intent import Intent
            intent = Intent(mode="persona_edit", use_persona=True, prompt_delta=f"早晨自拍，{outfit}，自然的晨光，轻松的表情", caption="早安", raw="早安")
            data, ext, _ = await self.plugin._generate(intent, user_key=f"morning:{target['umo']}", is_admin=True)
            path = self.plugin.storage.save_asset(data, ext)
            await self.plugin.context.send_message(target["umo"], MessageChain().message("早安，今天也要好好休息和吃饭。").file_image(str(path)))
        except Exception as exc:
            logger.warning("[人设影像] 早安消息失败：%s", exc)
