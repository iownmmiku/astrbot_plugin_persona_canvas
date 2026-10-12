from __future__ import annotations

import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Moderation:
    def __init__(self, settings, storage=None):
        self.settings, self.storage = settings, storage
        self._local, self._reservations, self._last = {}, {}, {}
        self._lock = threading.RLock()

    def check_content(self, text):
        if re.search(r"未成年.{0,8}(?:色情|裸体|性行为)|儿童.{0,8}(?:色情|裸体|性行为)", text):
            raise ValueError("该请求不允许生成")

    def allow(self, key, text, *, is_admin=False):
        allowed, reason, _token = self.reserve(key, text, is_admin=is_admin)
        return allowed, reason

    def reserve(self, key, text, *, is_admin=False):
        """Reserve one generation; retain its token until completion."""
        try:
            self.check_content(text)
        except ValueError as exc:
            return False, str(exc), None
        limits = self.settings.get("moderation", {})
        if self.storage:
            return self.storage.reserve_quota_token(key, limits, is_admin=is_admin)
        # Standalone completion also follows the reservation's original day.
        now = time.time()
        tz_name = str(limits.get("timezone") or "Asia/Shanghai")
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            fallbacks = {"Asia/Shanghai": timezone(timedelta(hours=8)), "Asia/Hong_Kong": timezone(timedelta(hours=8)), "UTC": timezone.utc, "Etc/UTC": timezone.utc}
            if tz_name not in fallbacks:
                raise ValueError("该时区在当前 Python 环境不可用")
            tz = fallbacks[tz_name]
        day = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")
        with self._lock:
            self._reservations = {token: record for token, record in self._reservations.items() if record["expires_at"] > now}
            running = [record for record in self._reservations.values() if record["key"] == key and not record["is_admin"]]
            if not is_admin and limits.get("enabled", True):
                if len(running) >= max(1, int(limits.get("max_concurrency", 1))):
                    return False, "已有图片正在生成", None
                limit = max(0, int(limits.get("daily_limit", 5)))
                if limit and self._local.get((key, day), 0) + sum(record["day"] == day for record in running) >= limit:
                    return False, "今天的生图次数已用完", None
                interval = max(0, int(limits.get("min_interval_sec", 20)))
                last = self._last.get(key, 0)
                if last and now - last < interval:
                    return False, "请稍后再拍摄", None
            token = secrets.token_hex(12)
            lease = max(60, int(limits.get("reservation_timeout_sec", max(720, int(self.settings.get("generation", {}).get("timeout_sec", 180)) + 120))))
            self._reservations[token] = {"key": key, "day": day, "is_admin": is_admin, "expires_at": now + lease}
            if not is_admin:
                self._last[key] = now
            return True, "", token

    def finish(self, key, *, success=True, token=None):
        if self.storage:
            if token is not None:
                self.storage.finish_quota_token(token, success=success)
            else:
                self.storage.finish_quota(key, success=success)
            return
        with self._lock:
            if token is None:
                token = next((candidate for candidate, record in self._reservations.items() if record["key"] == key), None)
            record = self._reservations.get(token)
            if record is None or record["key"] != key:
                return
            del self._reservations[token]
            if success and not record["is_admin"]:
                identity = (key, record["day"])
                self._local[identity] = self._local.get(identity, 0) + 1
