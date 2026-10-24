"""Persistent proactive deliveries; role decision always precedes generation."""
from __future__ import annotations
import asyncio
import json
import math
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from astrbot.api import logger
from astrbot.api.event import MessageChain
from .dialogue import field, maybe_await
from .temporal import temporal_conflicts, time_brief

class ActiveScheduler:
    def __init__(self, plugin):
        self.plugin = plugin
        self._tick_lock = asyncio.Lock()
        self._target_tasks = {}
        self._cursor = 0

    def observe(self, event):
        umo = str(event.unified_msg_origin or "")
        if not umo:
            return
        private = bool(event.is_private_chat())
        scope = "private" if private else "group"
        targets = self.plugin.storage.targets["items"]
        target = next((t for t in targets if t.get("umo") == umo), None)
        if target is None:
            target = {"umo": umo, "scope": scope, "sender_id": str(event.get_sender_id()), "platform": str(event.get_platform_id()), "enabled": False, "morning_enabled": private, "with_image": True, "unanswered": 0, "last_active": 0, "silent_until": 0, "morning_date": "", "reply_image_enabled": False, "reply_image_probability": self.plugin.storage.settings["reply_images"].get("default_probability", 20)}
            targets.append(target)
        target["scope"] = scope
        platform_name = getattr(event, "get_platform_name", None)
        if callable(platform_name):
            try:
                target["platform_type"] = str(platform_name() or "")[:100]
            except Exception:
                pass
        platform_name = getattr(event, "get_platform_name", lambda: "")()
        if platform_name:
            target["platform_name"] = str(platform_name)[:100]
        group_id = getattr(event, "get_group_id", lambda: "")() if not private else ""
        display_name = getattr(event, "get_sender_name", lambda: "")() if private else ""
        target["name"] = str(display_name or target.get("name") or ("群聊 " + str(group_id or umo.rsplit(":", 1)[-1]) if not private else "私聊 " + str(event.get_sender_id())))[:200]
        if not private:
            target["enabled"], target["morning_enabled"] = False, False
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
        if not exists or target.get("scope") == "group" or not self.plugin.storage.settings["integration"].get("enabled", True) or not target.get("enabled") or now < float(target.get("silent_until", 0)):
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

    def _time_context(self, target, kind):
        config = self.plugin.storage.settings["good_morning" if kind == "morning" else "active"]
        timezone = str(target.get("timezone") or config.get("timezone", "Asia/Shanghai"))
        now = datetime.now(ZoneInfo(timezone))
        period = ("凌晨" if now.hour < 5 else "上午" if now.hour < 11 else "中午" if now.hour < 13 else "下午" if now.hour < 18 else "晚上" if now.hour < 22 else "深夜")
        result = {"timezone": timezone, "now": now.isoformat(timespec="seconds"), "date": now.date().isoformat(), "weekday": "星期" + "一二三四五六日"[now.weekday()], "period": period, "last_inbound_at": None, "elapsed_seconds": None, "day_offset": None}
        try:
            inbound = float(target.get("last_inbound", 0))
            if math.isfinite(inbound) and 0 < inbound <= now.timestamp():
                previous = datetime.fromtimestamp(inbound, ZoneInfo(timezone))
                result.update(last_inbound_at=previous.isoformat(timespec="seconds"), elapsed_seconds=int(now.timestamp() - inbound), day_offset=(now.date() - previous.date()).days)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        return result

    def _check_time_context(self, target, kind, key, env, job=None):
        initial = env["proactive_context"]["time"]
        current = self._time_context(target, kind)
        elapsed = (datetime.fromisoformat(current["now"]) - datetime.fromisoformat(initial["now"])).total_seconds()
        if all(current[name] == initial[name] for name in ("timezone", "date", "period")) and 0 <= elapsed <= 900:
            return True
        reason = "主动内容的日期、时段或时区已改变，或决策已超过15分钟；本次不发送过期内容"
        self.plugin.storage.update_delivery(key, {"status": "skipped", "retryable": False, "skip_reason": "time_context_expired", "reason": reason})
        if job:
            self.plugin._photo_note(self.plugin._trace_job(job["id"], "time", "skipped", reason + "，原图已保留"))
        self.plugin.storage.record_action({"kind": "delivery", "source": kind, "umo": target["umo"], "session_key": env["key"], "persona_id": env["persona"]["id"], "status": "skipped", "summary": reason, "trace": [{"stage": "time", "status": "skipped", "detail": reason}]})
        return False

    def _image_policy(self, target, kind, *, check_budget=True):
        storage = self.plugin.storage
        mode = str(storage.settings["good_morning"].get("image_mode", "prefer_photo")) if kind == "morning" else "auto"
        reason = ""
        if not target.get("with_image", True):
            reason = "目标未允许主动图片"
        elif mode == "text_only":
            reason = "早安设置为仅文字"
        elif check_budget and storage.budget_summary(storage.settings["proactive_budget"])["photos"]["remaining"] <= 0:
            reason = "今日主动照片预算已用完"
        else:
            try:
                provider = self.plugin._provider()
                if not getattr(provider.capabilities, "automated_generation", True):
                    reason = "当前绘图接口不支持自动生成"
                elif not getattr(provider.capabilities, "text_to_image", True):
                    reason = "当前绘图接口不支持人设拍摄"
            except Exception as exc:
                reason = "绘图接口暂不可用：" + self.plugin._error(exc)
        return not reason, mode, reason

    async def _review_reply_time(self, target, kind, key, env, decision, outfit):
        clock = self._time_context(target, kind)
        review = {"status": "ok", "checked_at": clock["now"], "timezone": clock["timezone"], "issues": []}
        if decision["decision"] not in {"chat", "photo"}:
            return decision, review
        issues = temporal_conflicts(decision["reply"], clock, morning=kind == "morning")
        if not issues:
            return decision, review
        review.update(status="blocked", issues=issues, original_reply=decision["reply"])
        def blocked(reason):
            review["reason"] = reason
            return {**decision, "decision": "skip", "reply": "", "prompt": ""}, review
        initial_inbound = (self.plugin.storage.delivery(key) or {}).get("inbound_at", 0)
        if target.get("last_inbound", 0) != initial_inbound or not self._still_allowed(target, kind) or not self._check_time_context(target, kind, key, env):
            return blocked("检测到时间冲突，但联系条件已变化，本次不再修正或发送")
        budget = self.plugin.storage.reserve_budget("llm", self.plugin.storage.settings["proactive_budget"])
        if not budget:
            return blocked("时间冲突修正所需的模型预算不足，本次不发送")
        self.plugin.storage.finish_budget(budget, used=True)
        # Do not feed the old conversation back into the correction. The model
        # gets the current clock, current persona/schedule and one proposed draft.
        correction_env = {**env, "contexts": [], "proactive_context": {**env["proactive_context"], "time": clock}}
        prompt = "以下是你本轮准备主动发出的草稿，存在明确的时间冲突。请根据真实当地时钟只修正本轮内容，不接续旧历史的时间；不要求用户重新说关键词。保留角色口吻及原拍摄条件；原来是chat时不得改成photo/scene/edit，原来是photo时可继续photo或改为chat/skip/refuse。不强行拍照，不声称用户当前的作息或活动。若无法自然修正，选择skip。返回相同格式的决策JSON。\n" + json.dumps({"conflicts": issues, "draft": decision}, ensure_ascii=False)
        try:
            corrected = await self.plugin.dialogue.decide(correction_env, prompt, proactive=True, event_kind="早安" if kind == "morning" else "日常", outfit=outfit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return blocked("时间冲突修正失败，本次不发送：" + self.plugin._error(exc))
        finally:
            self.plugin.storage.finish_budget(budget)
        if corrected["decision"] in {"scene", "edit"} or (decision["decision"] == "chat" and corrected["decision"] == "photo"):
            return blocked("时间修正试图扩大原动作范围，本次不发送")
        current = self._time_context(target, kind)
        remaining = temporal_conflicts(corrected["reply"], current, morning=kind == "morning") if corrected["decision"] in {"chat", "photo"} else []
        if remaining:
            review["remaining_issues"] = remaining
            return blocked("修正后仍有时间冲突，本次不发送")
        corrected["requirements"] = {**corrected.get("requirements", {}), **decision.get("requirements", {})}
        review.update(status="corrected", checked_at=current["now"], reply=corrected["reply"])
        return corrected, review

    def _check_reply_time(self, target, kind, key, env, decision, job=None):
        clock = self._time_context(target, kind)
        issues = temporal_conflicts(decision["reply"], clock, morning=kind == "morning")
        if not issues:
            return True
        reason = "发送前时间校验未通过：" + "；".join(issues)
        self.plugin.storage.update_delivery(key, {"status": "skipped", "retryable": False, "skip_reason": "time_conflict", "reason": reason})
        if job:
            self.plugin._photo_note(self.plugin._trace_job(job["id"], "time_validation", "skipped", reason + "，原图已保留"))
        self.plugin.storage.record_action({"kind": "delivery", "source": kind, "umo": target["umo"], "session_key": env["key"], "persona_id": env["persona"]["id"], "status": "skipped", "summary": reason, "trace": [{"stage": "time_validation", "status": "skipped", "detail": reason}]})
        return False

    async def _schedule_context(self, target):
        """Use the optional schedule plugin's public API, never its storage."""
        discover = getattr(self.plugin.context, "get_registered_star", None)
        if not callable(discover):
            return None
        try:
            metadata = await maybe_await(discover("astrbot_plugin_persona_schedule"))
            if metadata is None or not field(metadata, "activated", True):
                return None
            method = getattr(field(metadata, "star_cls"), "get_proactive_context", None)
            if not callable(method):
                # An absent/older plugin does not change existing behaviour.
                return None
            state = await asyncio.wait_for(maybe_await(method(target["umo"])), 15)
            if not isinstance(state, dict) or not isinstance(state.get("applicable"), bool):
                raise ValueError("Invalid schedule response")
            if not state["applicable"]:
                return None
            if not isinstance(state.get("allowed"), bool):
                raise ValueError("Invalid schedule permission")
            if state["allowed"] and (not isinstance(state.get("context_key"), str) or not state["context_key"]):
                raise ValueError("Missing schedule identity")
            if state["allowed"] and (not isinstance(state.get("persona_id"), str) or not state["persona_id"] or not isinstance(state.get("cid"), str)):
                raise ValueError("Missing schedule conversation")
            return {**state, "allowed": state["allowed"] and state.get("status") != "busy", "prompt": str(state.get("prompt") or "")[:12000]}
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fixed text avoids exposing provider credentials from exceptions.
            # Discard this opportunity; do not create a deferred user reply.
            return {"applicable": True, "allowed": False, "status": "unavailable", "reason": "日程联动状态暂不可用，本次主动联系已取消"}

    def _skip_schedule(self, target, kind, key, state, env=None, job=None):
        storage = self.plugin.storage
        reason = str(state.get("reason") or "当前日程不允许主动联系，本次机会已取消")[:500]
        storage.update_delivery(key, {"status": "skipped", "retryable": False, "skip_reason": "schedule_cancelled", "reason": reason})
        if job:
            cancelled = storage.update_job(job["id"], {"status": "cancelled", "cancel_requested": True, "error": reason})
            self.plugin._photo_note(self.plugin._trace_job(cancelled["id"], "schedule", "cancelled", reason))
        storage.record_action({"kind": "decision", "source": kind, "session_key": (env or {}).get("key", target["umo"]), "persona_id": ((env or {}).get("persona") or {}).get("id", ""), "umo": target["umo"], "status": "skip", "reply": "", "summary": reason, "trace": [{"stage": "schedule", "status": "skipped", "detail": reason}]}, "schedule:" + key)

    async def _check_schedule(self, target, kind, key, initial, env=None, job=None):
        if initial:
            try:
                identity = await asyncio.wait_for(self.plugin.dialogue.current_identity(target["umo"]), 15)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._skip_schedule(target, kind, key, {"reason": "当前会话身份暂不可用，本次主动联系已取消"}, env, job)
                return False
        # Read schedule last: identity resolution may yield while an admin
        # edits a time slot. No further await precedes the send decision.
        current = await self._schedule_context(target)
        if current and not current["allowed"]:
            self._skip_schedule(target, kind, key, current, env, job)
            return False
        if initial and current is None:
            self._skip_schedule(target, kind, key, {"reason": "日程联动范围或启用状态已改变，本次主动联系已取消"}, env, job)
            return False
        if current and initial is None:
            self._skip_schedule(target, kind, key, {"reason": "日程联动已在生成期间启用，本次主动联系已取消"}, env, job)
            return False
        if initial and current:
            if initial["context_key"] != current["context_key"]:
                self._skip_schedule(target, kind, key, {"reason": "人设、会话或当前日程已改变，本次主动联系已取消"}, env, job)
                return False
            if any(str((env or {}).get(key) or "") != str(current[field] or "") or identity[key] != str(current[field] or "") for key, field in (("astro_id", "persona_id"), ("cid", "cid"))):
                self._skip_schedule(target, kind, key, {"reason": "画卷与当前 AstrBot 人设或会话不一致，本次主动联系已取消"}, env, job)
                return False
        return True

    def _attempt_limit(self, kind):
        if kind == "morning":
            return max(1, min(5, int(self.plugin.storage.settings["good_morning"].get("max_attempts", 3))))
        return 3

    def _same_morning_date(self, target, kind, date):
        if kind != "morning" or not date:
            return True
        config = self.plugin.storage.settings["good_morning"]
        timezone = str(target.get("timezone") or config.get("timezone", "Asia/Shanghai"))
        return datetime.now(ZoneInfo(timezone)).date().isoformat() == date

    def _retry_ready(self, target, kind, key, now):
        existing = self.plugin.storage.delivery(key)
        if not existing:
            return True
        role_skip = kind == "morning" and existing.get("status") == "skipped" and existing.get("retryable") is True and existing.get("skip_reason") == "role_skip"
        retryable = existing.get("status") == "failed" or role_skip
        if kind == "morning" and retryable and "inbound_at" in existing and existing["inbound_at"] != target.get("last_inbound", 0):
            self.plugin.storage.update_delivery(key, {"status": "skipped", "retryable": False, "skip_reason": "user_replied", "reason": "用户已发来消息，不再补发本次早安"})
            return False
        if existing.get("status") != "failed" and not role_skip:
            return False
        return int(existing.get("attempts", 1)) < self._attempt_limit(kind) and existing.get("retry_at") is not None and now >= float(existing["retry_at"])

    @staticmethod
    def _interrupted_status(state, job):
        # The send operation owns the delivery outcome. Keep a retained image
        # out of automatic generation retries, including when cancellation
        # arrives after the platform send has already started.
        outcomes = {state.get("status"), job.get("status")}
        if "uncertain" in outcomes:
            return "uncertain"
        if "delivery_failed" in outcomes:
            return "delivery_failed"
        if "sending" in outcomes:
            return "uncertain"
        return "skipped" if job.get("cancel_requested") else "failed"

    async def _attempt(self, target, kind, key, now, date=""):
        try:
            if self._still_allowed(target, kind):
                await self._deliver(target, kind, key, now, date)
        except asyncio.CancelledError:
            # The task belongs to one target; cancelling its photo does not
            # cancel a scheduler tick or any other target's opportunity.
            raise
        except Exception as exc:
            target["last_error"] = self.plugin._error(exc)
            self.plugin.storage.save_targets()

    def _start_attempt(self, target, kind, key, now, date=""):
        umo = target["umo"]
        task = self.plugin._spawn(self._attempt(target, kind, key, now, date), "persona-canvas-active-attempt")
        self._target_tasks[umo] = task
        def release(completed):
            if self._target_tasks.get(umo) is completed:
                self._target_tasks.pop(umo, None)
        task.add_done_callback(release)
        return task

    async def tick(self, *, wait=False):
        """Dispatch bounded per-target work without waiting in the tick lock.

        ``wait`` is useful for a one-shot check; the background scheduler keeps
        dispatching while slow deliveries are still running. Generation itself
        continues to use the plugin's shared image-generation slots and budgets.
        """
        started = []
        async with self._tick_lock:
            settings = self.plugin.storage.settings
            if getattr(self.plugin, "_closing", False) or not settings["integration"].get("enabled", True):
                return
            now = time.time()
            self._target_tasks = {umo: task for umo, task in self._target_tasks.items() if not task.done()}
            capacity = max(1, min(16, int(settings["active"].get("max_concurrent_targets", 3))))
            targets = list(self.plugin.storage.targets["items"])
            count = len(targets)
            offset = self._cursor % count if count else 0
            for index in range(count):
                if len(self._target_tasks) >= capacity:
                    break
                position = (offset + index) % count
                target = targets[position]
                if target.get("umo") in self._target_tasks:
                    continue
                if self._blocked(target, now):
                    continue
                try:
                    morning, active = settings["good_morning"], settings["active"]
                    timezone = str(target.get("timezone") or morning.get("timezone", "Asia/Shanghai"))
                    if morning.get("enabled") and target.get("morning_enabled", True) and self._in_window(target.get("morning_start") or morning["start"], target.get("morning_end") or morning["end"], timezone):
                        date = datetime.now(ZoneInfo(timezone)).date().isoformat()
                        key = f"morning:{target['umo']}:{date}"
                        if target.get("morning_date") != date and self._retry_ready(target, "morning", key, now):
                            started.append(self._start_attempt(target, "morning", key, now, date))
                            self._cursor = (position + 1) % count
                    elif active.get("enabled") and self._in_window(target.get("start") or active["default_start"], target.get("end") or active["default_end"], str(target.get("timezone") or active.get("timezone", "Asia/Shanghai"))):
                        previous = self.plugin.storage.delivery(target.get("active_delivery_key", ""))
                        retry = previous and previous.get("status") == "failed" and int(previous.get("attempts", 0)) < 3
                        if retry:
                            started.append(self._start_attempt(target, "active", previous["key"], now))
                            self._cursor = (position + 1) % count
                        elif now >= float(target.get("next_opportunity", 0)):
                            key = f"active:{target['umo']}:{int(now * 1000)}"
                            target.update(active_delivery_key=key, next_opportunity=self._next_opportunity(target, now))
                            self.plugin.storage.save_targets()
                            started.append(self._start_attempt(target, "active", key, now))
                            self._cursor = (position + 1) % count
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    target["last_error"] = self.plugin._error(exc)
                    self.plugin.storage.save_targets()
        if wait and started:
            await asyncio.gather(*started, return_exceptions=True)

    async def _deliver(self, target, kind, key, now, date=""):
        storage = self.plugin.storage
        if not self._same_morning_date(target, kind, date):
            return
        if date and target.get("morning_date") == date:
            return
        existing = storage.delivery(key)
        if existing:
            if not self._retry_ready(target, kind, key, now):
                return
            if not storage.claim_delivery_retry(key, now=now, max_attempts=self._attempt_limit(kind), allow_role_skip=kind == "morning"):
                return
        elif storage.reserve_delivery(key):
            storage.update_delivery(key, {"attempts": 1, "kind": kind, "umo": target["umo"], "inbound_at": target.get("last_inbound", 0)})
        else:
            return
        initial_inbound = target.get("last_inbound", 0)
        message_budget = None
        model_budget = None
        try:
            schedule = await self._schedule_context(target)
            if schedule and not schedule["allowed"]:
                self._skip_schedule(target, kind, key, schedule)
                return
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
            if schedule:
                if str(env.get("astro_id") or "") != schedule["persona_id"] or str(env.get("cid") or "") != schedule["cid"]:
                    self._skip_schedule(target, kind, key, {"reason": "画卷与日程的人设或会话不一致，本次主动联系已取消"}, env)
                    return
                env["schedule_context"] = schedule["prompt"]
            outfit = random.choice(env["persona"].get("outfit_pool") or ["casual morning clothes"]) if kind == "morning" else ""
            image_allowed, image_mode, image_reason = self._image_policy(target, kind)
            env["proactive_context"] = {"time": self._time_context(target, kind), "image_allowed": image_allowed, "morning_image_mode": image_mode, "image_unavailable_reason": image_reason}
            text = ("现在处于管理员开启的每日早安时间窗。请用当前角色的口吻自然问候用户；即使近期没有未完话题，也可以简短说早安。按本次早安配图模式决定是否拍照；暂时跳过选skip，用户要求安静或你明确今天不愿打扰时选refuse。" if kind == "morning" else "现在有一次自然联系用户的机会。结合近期聊天和当前时间决定是否发送，不补发昨晚过期的问候。") + ("本次不允许照片，不要选择照片、不要声称已拍照。" if not image_allowed else "")
            recent = [{"status": item["status"], "reply": item.get("reply", ""), "at": item["at"]} for item in storage.recent_actions(env["key"], 30) if item.get("source") in {"active", "morning"}][:4]
            text += ("可接续近期话题，但没有话题不等于不能问候。避免重复已经发送的问候；之前的skip不是已发送。近期主动尝试：" if kind == "morning" else "优先接续最近聊天中的话题，避免重复最近的问候；没有合适话题时选择skip。近期主动尝试：") + json.dumps(recent, ensure_ascii=False)
            # Count attempts, including timeouts; paid calls cannot be rolled back.
            storage.finish_budget(model_budget, used=True)
            decision = await self.plugin.dialogue.decide(env, text, proactive=True, event_kind="早安" if kind == "morning" else "日常", outfit=outfit)
            decision, time_review = await self._review_reply_time(target, kind, key, env, decision, outfit)
            attempt = int((storage.delivery(key) or {}).get("attempts", 1))
            action = storage.record_action({"kind": "decision", "source": kind, "session_key": env["key"], "persona_id": env["persona"]["id"], "umo": target["umo"], "status": decision["decision"], "reply": decision["reply"], "summary": f"早安第 {attempt}/{self._attempt_limit(kind)} 次尝试" if kind == "morning" else "日常主动联系机会", "requirements": decision.get("requirements", {}), "trace": [{"stage": "schedule", "status": "ok", "detail": "时间、空闲、静默与预算检查通过"}, {"stage": "role", "status": decision["decision"], "detail": decision["reply"] or "角色选择本轮不联系，没有生成或发送"}]})
            action["trace"].append({"stage": "time", "status": "ok", "detail": json.dumps(env["proactive_context"], ensure_ascii=False)})
            action["trace"].append({"stage": "time_validation", "status": time_review["status"], "detail": json.dumps(time_review, ensure_ascii=False)})
            storage.record_action({"trace": action["trace"]}, action["id"])
            storage.update_delivery(key, {"status": "decided", "decision": decision, "proactive_context": env["proactive_context"], "time_review": time_review})
            if kind == "morning" and not await self._check_schedule(target, kind, key, schedule, env):
                return
            # Schedule/identity resolution may yield across midnight. Check the
            # date after that last await, including before a skip can be retried.
            if not self._same_morning_date(target, kind, date):
                storage.update_delivery(key, {"status": "skipped", "retryable": False, "skip_reason": "date_changed", "reason": "早安所属日期已改变，不发送前一天的内容"})
                return
            if not self._check_time_context(target, kind, key, env):
                return
            if time_review["status"] == "blocked":
                storage.update_delivery(key, {"status": "skipped", "retryable": False, "skip_reason": "time_conflict", "reason": time_review["reason"]})
                storage.record_action({"summary": action["summary"] + " · " + time_review["reason"]}, action["id"])
                return
            if decision["decision"] in {"skip", "refuse", "ask", "state"}:
                retryable = kind == "morning" and decision["decision"] == "skip" and attempt < self._attempt_limit(kind)
                interval = max(60, min(3600, int(storage.settings["good_morning"].get("retry_interval_sec", 900))))
                storage.update_delivery(key, {"status": "skipped", "retryable": retryable, "skip_reason": "role_skip" if decision["decision"] == "skip" else "role_" + decision["decision"], "inbound_at": initial_inbound, "retry_at": time.time() + interval if retryable else 0, "reason": "角色本轮暂不联系，将在早安窗口内限次重试" if retryable else "角色选择不联系或已达到尝试上限"})
                if kind == "morning":
                    detail = f"本轮未发送；{interval} 秒后可在早安窗口内再次判断" if retryable else "本轮未发送；明确不联系或尝试次数已用完，不再重试"
                    action["trace"].append({"stage": "schedule", "status": "skipped", "detail": detail})
                    storage.record_action({"summary": action["summary"] + " · " + detail, "trace": action["trace"]}, action["id"])
                return
            if kind != "morning" and not await self._check_schedule(target, kind, key, schedule, env):
                return
            if not self._check_time_context(target, kind, key, env):
                return
            if not self._check_reply_time(target, kind, key, env, decision):
                return
            if target.get("last_inbound", 0) != initial_inbound or not self._still_allowed(target, kind):
                storage.update_delivery(key, {"status": "skipped", "reason": "用户已回复，或目标、时间窗、启用状态与静默条件已经改变"})
                return
            webchat_delivery = self.plugin._is_webchat(target["umo"], job={"delivery_platform": target.get("platform_type", "")})
            delivery_unconfirmed = False
            if decision["decision"] in {"photo", "scene", "edit"}:
                current_image_allowed, _, current_image_reason = self._image_policy(target, kind)
                if not image_allowed or not current_image_allowed or decision["decision"] != "photo":
                    storage.update_delivery(key, {"status": "skipped", "retryable": False, "skip_reason": "image_unavailable", "reason": current_image_reason or image_reason or "主动照片只支持已启用的人设拍摄"})
                    return
                job = storage.create_job({"status": "queued", "source": kind, "delivery_platform": target.get("platform_type", ""), "delivery_key": key, "umo": env["umo"], "session_key": env["key"], "persona_id": env["persona"]["id"], "caption": decision["reply"], "mode": "persona"})
                storage.update_delivery(key, {"status": "generating", "job_id": job["id"]})
                env["approved_requirements"] = decision.get("requirements", {})
                job = await self.plugin._generate_job(env, decision["prompt"], "persona", decision["state_patch"], decision["reply"], "主动早安" if kind == "morning" else "主动照片", user_key="active:" + target["umo"], is_admin=True, source=kind, job_id=job["id"])
                if not await self._check_schedule(target, kind, key, schedule, env, job):
                    return
                if not self._check_time_context(target, kind, key, env, job):
                    return
                if not self._check_reply_time(target, kind, key, env, decision, job):
                    return
                if not self._image_policy(target, kind, check_budget=False)[0]:
                    storage.update_delivery(key, {"status": "skipped", "retryable": False, "skip_reason": "image_unavailable", "reason": "生成期间主动图片设置或接口能力改变，原图已保留，本次不发送"})
                    return
                if target.get("last_inbound", 0) != initial_inbound or not self._still_allowed(target, kind) or not self._same_morning_date(target, kind, date):
                    storage.update_delivery(key, {"status": "skipped", "retryable": False, "reason": "生成期间用户已回复，或目标、日期与发送条件已经改变"})
                    return
                storage.update_delivery(key, {"status": "sending"})
                storage.finish_budget(message_budget, used=True)
                delivered = await self.plugin._send_job(job, umo=target["umo"])
                delivery_unconfirmed = (delivered or storage.job(job["id"]) or {}).get("status") == "uncertain"
            else:
                storage.update_delivery(key, {"status": "sending"})
                storage.finish_budget(message_budget, used=True)
                accepted = await self.plugin.context.send_message(target["umo"], MessageChain().message(decision["reply"]))
                if accepted is False:
                    storage.update_delivery(key, {"status": "delivery_failed"})
                    raise ValueError("平台没有接受主动消息")
            if delivery_unconfirmed or webchat_delivery:
                note = "WebChat 会话发送调用已完成，但实时投递或历史保存未确认；不自动补发"
                storage.update_delivery(key, {"status": "uncertain", "error": note, "delivery_evidence": "unverified"})
                target.update(last_active=time.time(), unanswered=int(target.get("unanswered", 0)) + 1, last_error=note, next_opportunity=self._next_opportunity(target, time.time()))
                storage.save_targets()
                storage.record_action({"kind": "delivery", "source": kind, "umo": target["umo"], "session_key": env["key"], "persona_id": env["persona"]["id"], "status": "uncertain", "summary": note, "trace": [{"stage": "delivery", "status": "uncertain", "detail": note}]})
                return
            storage.update_delivery(key, {"status": "sent"})
            target.update(last_active=time.time(), unanswered=int(target.get("unanswered", 0)) + 1, last_error="", next_opportunity=self._next_opportunity(target, time.time()))
            if date:
                target["morning_date"] = date
            storage.save_targets()
            try:
                sent_clock = self._time_context(target, kind)
                await self.plugin.dialogue.remember(env, f"[系统主动联系机会：{kind}，不是用户请求；当地发送时间：{sent_clock['now']}；时区：{sent_clock['timezone']}]", decision["reply"] + (" [已发送照片：" + decision["prompt"] + "]" if decision["decision"] == "photo" else ""))
            except Exception as exc:
                target["last_error"] = "消息已发送，写入聊天记忆失败：" + self.plugin._error(exc)
                storage.save_targets()
        except asyncio.CancelledError:
            state = storage.delivery(key) or {}
            job = storage.job(state.get("job_id", "")) or {}
            withdrawn = job.get("cancel_requested")
            storage.update_delivery(key, {"status": self._interrupted_status(state, job), "error": "用户撤回拍摄" if withdrawn else "任务被停止", "retry_at": now + 300})
            raise
        except Exception as exc:
            state = storage.delivery(key) or {}
            job = storage.job(state.get("job_id", "")) or {}
            storage.update_delivery(key, {"status": self._interrupted_status(state, job), "error": self.plugin._error(exc), "retry_at": time.time() + 300})
            if job and job["status"] in {"queued", "generating"}:
                self.plugin._photo_note(storage.update_job(job["id"], {"status": "failed", "error": self.plugin._error(exc)}))
            target["last_error"] = self.plugin._error(exc)
            storage.save_targets()
        finally:
            storage.finish_budget(message_budget)
            storage.finish_budget(model_budget)
