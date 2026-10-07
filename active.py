"""Persistent proactive deliveries; role decision always precedes generation."""
from __future__ import annotations
import asyncio
import json
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from astrbot.api import logger
from astrbot.api.event import MessageChain

class ActiveScheduler:
    def __init__(self, plugin):
        self.plugin = plugin
        self._tick_lock = asyncio.Lock()

    def observe(self, event):
        umo = str(event.unified_msg_origin or "")
        if not umo:
            return
        targets = self.plugin.storage.targets["items"]
        target = next((t for t in targets if t.get("umo") == umo), None)
        if target is None:
            target = {"umo": umo, "sender_id": str(event.get_sender_id()), "platform": str(event.get_platform_id()), "enabled": False, "morning_enabled": True, "with_image": True, "unanswered": 0, "last_active": 0, "silent_until": 0, "morning_date": ""}
            targets.append(target)
        target.update(last_inbound=time.time(), unanswered=0, silent_until=0, next_opportunity=0)
        self.plugin.storage.save_targets()

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[随想画卷] 调度检查失败：%s", self.plugin._error(exc))
            await asyncio.sleep(max(15, int(self.plugin.storage.settings["active"].get("check_interval_sec", 30))))

    @staticmethod
    def _in_window(start, end, timezone="Asia/Shanghai"):
        now = datetime.now(ZoneInfo(timezone))
        left = datetime.strptime(start, "%H:%M").time()
        right = datetime.strptime(end, "%H:%M").time()
        current = now.time().replace(tzinfo=None)
        return left <= current <= right if left <= right else current >= left or current <= right

    def _blocked(self, target, now):
        active = self.plugin.storage.settings["active"]
        exists = any(item.get("umo") == target.get("umo") for item in self.plugin.storage.targets["items"])
        if not exists or not self.plugin.storage.settings["integration"].get("enabled", True) or not target.get("enabled") or now < float(target.get("silent_until", 0)):
            return True
        if target.get("silent_until"):
            # A completed silence period starts a new unanswered cycle.
            target.update(silent_until=0, unanswered=0)
            self.plugin.storage.save_targets()
        if now - float(target.get("last_inbound", 0)) < int(target.get("min_idle_sec", active.get("min_idle_sec", 1800))):
            return True
        if now - float(target.get("last_active", 0)) < int(target.get("min_gap_sec", active.get("min_gap_sec", 3600))):
            return True
        limit = int(target.get("silence_after", active.get("silence_after", 3)))
        if int(target.get("unanswered", 0)) >= limit:
            target["silent_until"] = now + int(active.get("silence_hours", 24)) * 3600
            self.plugin.storage.save_targets()
            return True
        return False

    def _still_allowed(self, target, kind):
        if self._blocked(target, time.time()):
            return False
        settings = self.plugin.storage.settings
        config = settings["good_morning" if kind == "morning" else "active"]
        if not config.get("enabled") or (kind == "morning" and not target.get("morning_enabled", True)):
            return False
        if kind == "morning":
            start, end = target.get("morning_start") or config["start"], target.get("morning_end") or config["end"]
        else:
            start, end = target.get("start") or config["default_start"], target.get("end") or config["default_end"]
        return self._in_window(start, end, str(target.get("timezone") or config.get("timezone", "Asia/Shanghai")))

    def _next_opportunity(self, target, now):
        active = self.plugin.storage.settings["active"]
        gap = max(60, int(target.get("min_gap_sec", active.get("min_gap_sec", 3600))))
        jitter = max(0, min(40, int(active.get("jitter_percent", 15)))) / 100
        return now + gap * (1 + random.uniform(0, jitter))

    async def _attempt(self, target, kind, key, now, date=""):
        # A cancelled photo must stop only this opportunity, not the scheduler.
        task = self.plugin._spawn(self._deliver(target, kind, key, now, date), "persona-canvas-active-attempt")
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise

    async def tick(self):
        async with self._tick_lock:
            settings = self.plugin.storage.settings
            if not settings["integration"].get("enabled", True):
                return
            now = time.time()
            for target in list(self.plugin.storage.targets["items"]):
                if self._blocked(target, now):
                    continue
                try:
                    morning, active = settings["good_morning"], settings["active"]
                    timezone = str(target.get("timezone") or morning.get("timezone", "Asia/Shanghai"))
                    if morning.get("enabled") and target.get("morning_enabled", True) and self._in_window(target.get("morning_start") or morning["start"], target.get("morning_end") or morning["end"], timezone):
                        date = datetime.now(ZoneInfo(timezone)).date().isoformat()
                        await self._attempt(target, "morning", f"morning:{target['umo']}:{date}", now, date)
                    elif active.get("enabled") and self._in_window(target.get("start") or active["default_start"], target.get("end") or active["default_end"], str(target.get("timezone") or active.get("timezone", "Asia/Shanghai"))):
                        previous = self.plugin.storage.delivery(target.get("active_delivery_key", ""))
                        retry = previous and previous.get("status") == "failed" and int(previous.get("attempts", 0)) < 3
                        if retry:
                            await self._attempt(target, "active", previous["key"], now)
                        elif now >= float(target.get("next_opportunity", 0)):
                            key = f"active:{target['umo']}:{int(now * 1000)}"
                            target.update(active_delivery_key=key, next_opportunity=self._next_opportunity(target, now))
                            self.plugin.storage.save_targets()
                            await self._attempt(target, "active", key, now)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    target["last_error"] = self.plugin._error(exc)
                    self.plugin.storage.save_targets()

    async def _deliver(self, target, kind, key, now, date=""):
        storage = self.plugin.storage
        existing = storage.delivery(key)
        if existing:
            if existing.get("status") != "failed" or int(existing.get("attempts", 0)) >= 3 or now < float(existing.get("retry_at", 0)):
                return
            storage.update_delivery(key, {"status": "claimed", "attempts": int(existing.get("attempts", 0)) + 1})
        elif storage.reserve_delivery(key):
            storage.update_delivery(key, {"attempts": 1, "kind": kind, "umo": target["umo"]})
        else:
            return
        initial_inbound = target.get("last_inbound", 0)
        message_budget = None
        model_budget = None
        try:
            limits = storage.settings["proactive_budget"]
            message_budget = storage.reserve_budget("messages", limits)
            if not message_budget:
                storage.update_delivery(key, {"status": "skipped", "reason": "今日主动消息预算已用完"})
                return
            model_budget = storage.reserve_budget("llm", limits)
            if not model_budget:
                storage.update_delivery(key, {"status": "skipped", "reason": "今日主动判断模型预算已用完"})
                return
            env = await self.plugin.dialogue.environment(target["umo"], target.get("persona_id", ""))
            outfit = random.choice(env["persona"].get("outfit_pool") or ["casual morning clothes"]) if kind == "morning" else ""
            image_allowed = target.get("with_image", True) and storage.budget_summary(limits)["photos"]["remaining"] > 0
            text = "现在有一次自然联系用户的机会。结合近期聊天决定是否发送。" + ("本次只允许文字或skip，不要选择照片、不要声称已拍照。" if not image_allowed else "")
            recent = [{"status": item["status"], "reply": item.get("reply", ""), "at": item["at"]} for item in storage.recent_actions(env["key"], 30) if item.get("source") in {"active", "morning"}][:4]
            text += "优先接续最近聊天中的话题，避免重复最近的问候；没有合适话题时选择skip。近期主动尝试：" + json.dumps(recent, ensure_ascii=False)
            # Count attempts, including timeouts; paid calls cannot be rolled back.
            storage.finish_budget(model_budget, used=True)
            decision = await self.plugin.dialogue.decide(env, text, proactive=True, event_kind="早安" if kind == "morning" else "日常", outfit=outfit)
            storage.record_action({"kind": "decision", "source": kind, "session_key": env["key"], "persona_id": env["persona"]["id"], "umo": target["umo"], "status": decision["decision"], "reply": decision["reply"], "requirements": decision.get("requirements", {}), "trace": [{"stage": "schedule", "status": "ok", "detail": "时间、空闲、静默与预算检查通过"}, {"stage": "role", "status": decision["decision"], "detail": decision["reply"]}]})
            storage.update_delivery(key, {"status": "decided", "decision": decision})
            if decision["decision"] in {"skip", "refuse", "ask", "state"}:
                storage.update_delivery(key, {"status": "skipped"})
                return
            if target.get("last_inbound", 0) != initial_inbound or not self._still_allowed(target, kind):
                storage.update_delivery(key, {"status": "skipped", "reason": "用户已回复，或目标、时间窗、启用状态与静默条件已经改变"})
                return
            if decision["decision"] in {"photo", "scene", "edit"}:
                if not image_allowed or decision["decision"] != "photo":
                    raise ValueError("主动照片只支持已启用的人设拍摄")
                job = storage.create_job({"status": "queued", "source": kind, "delivery_key": key, "umo": env["umo"], "session_key": env["key"], "persona_id": env["persona"]["id"], "caption": decision["reply"], "mode": "persona"})
                storage.update_delivery(key, {"status": "generating", "job_id": job["id"]})
                env["approved_requirements"] = decision.get("requirements", {})
                job = await self.plugin._generate_job(env, decision["prompt"], "persona", decision["state_patch"], decision["reply"], "主动早安" if kind == "morning" else "主动照片", user_key="active:" + target["umo"], is_admin=True, source=kind, job_id=job["id"])
                if target.get("last_inbound", 0) != initial_inbound or not self._still_allowed(target, kind):
                    storage.update_delivery(key, {"status": "skipped", "reason": "生成期间用户已回复，或目标与发送条件已经改变"})
                    return
                storage.update_delivery(key, {"status": "sending"})
                storage.finish_budget(message_budget, used=True)
                await self.plugin._send_job(job, umo=target["umo"])
            else:
                storage.update_delivery(key, {"status": "sending"})
                storage.finish_budget(message_budget, used=True)
                accepted = await self.plugin.context.send_message(target["umo"], MessageChain().message(decision["reply"]))
                if accepted is False:
                    raise ValueError("平台没有接受主动消息")
            storage.update_delivery(key, {"status": "sent"})
            target.update(last_active=time.time(), unanswered=int(target.get("unanswered", 0)) + 1, last_error="", next_opportunity=self._next_opportunity(target, time.time()))
            if date:
                target["morning_date"] = date
            storage.save_targets()
            try:
                await self.plugin.dialogue.remember(env, f"[系统主动联系机会：{kind}，不是用户请求]", decision["reply"] + (" [已发送照片：" + decision["prompt"] + "]" if decision["decision"] == "photo" else ""))
            except Exception as exc:
                target["last_error"] = "消息已发送，写入聊天记忆失败：" + self.plugin._error(exc)
                storage.save_targets()
        except asyncio.CancelledError:
            state = storage.delivery(key) or {}
            job = storage.job(state.get("job_id", "")) or {}
            withdrawn = job.get("cancel_requested")
            storage.update_delivery(key, {"status": "skipped" if withdrawn else "uncertain" if state.get("status") == "sending" else "failed", "error": "用户撤回拍摄" if withdrawn else "任务被停止", "retry_at": now + 300})
            raise
        except Exception as exc:
            state = storage.delivery(key) or {}
            uncertain = state.get("status") == "sending"
            storage.update_delivery(key, {"status": "uncertain" if uncertain else "failed", "error": self.plugin._error(exc), "retry_at": time.time() + 300})
            job = storage.job(state.get("job_id", "")) or {}
            if job and job["status"] in {"queued", "generating"}:
                self.plugin._photo_note(storage.update_job(job["id"], {"status": "failed", "error": self.plugin._error(exc)}))
            target["last_error"] = self.plugin._error(exc)
            storage.save_targets()
        finally:
            storage.finish_budget(message_budget)
            storage.finish_budget(model_budget)
