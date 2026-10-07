from __future__ import annotations
import re
import time

class Moderation:
    def __init__(self, settings, storage=None):
        self.settings, self.storage = settings, storage
        self._local = {}

    def check_content(self, text):
        if re.search(r"未成年.{0,8}(?:色情|裸体|性行为)|儿童.{0,8}(?:色情|裸体|性行为)", text):
            raise ValueError("该请求不允许生成")

    def allow(self, key, text, *, is_admin=False):
        try:
            self.check_content(text)
        except ValueError as exc:
            return False, str(exc)
        limits = self.settings.get("moderation", {})
        if self.storage:
            return self.storage.reserve_quota(key, limits, is_admin=is_admin)
        if is_admin or not limits.get("enabled", True):
            return True, ""
        record = self._local.setdefault(key, {"day": time.strftime("%Y-%m-%d"), "count": 0, "running": 0, "last": 0})
        day = time.strftime("%Y-%m-%d")
        if day != record["day"]:
            record.update(day=day, count=0)
        if record["running"] >= int(limits.get("max_concurrency", 1)):
            return False, "已有图片正在生成"
        limit = int(limits.get("daily_limit", 5))
        if limit and record["count"] + record["running"] >= limit:
            return False, "今天的生图次数已用完"
        if time.time() - record["last"] < int(limits.get("min_interval_sec", 20)):
            return False, "请稍后再拍摄"
        record["running"] += 1
        return True, ""

    def finish(self, key, *, success=True):
        if self.storage:
            self.storage.finish_quota(key, success=success)
        elif key in self._local:
            record = self._local[key]
            record["running"] = max(0, record["running"] - 1)
            if success:
                record["count"] += 1
                record["last"] = time.time()
