from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any


# These are intentionally conservative local checks. They prevent obvious unsafe
# requests even when the optional LLM classifier is unavailable.
HARD_BLOCKS = (
    r"未成年.{0,12}(色情|裸|性|淫|自慰)",
    r"(儿童|小孩|幼女|幼童).{0,12}(裸|色情|性行为)",
    r"(偷拍|强奸|非自愿).{0,12}(裸|色情|性)",
    r"(制作|生成).{0,12}(炸弹|毒品|病毒)",
)


class Moderation:
    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.calls: dict[str, list[float]] = defaultdict(list)
        self.inflight: dict[str, int] = defaultdict(int)

    def hard_block(self, text: str) -> str | None:
        for pattern in HARD_BLOCKS:
            if re.search(pattern, text, re.I):
                return "这类内容不能生成。"
        return None

    def allow(self, key: str, text: str, *, is_admin: bool = False) -> tuple[bool, str]:
        blocked = self.hard_block(text)
        if blocked:
            return False, blocked
        if is_admin:
            return True, ""
        config = self.settings.get("moderation") or {}
        now = time.time()
        limit = max(0, int(config.get("daily_limit", 5)))
        interval = max(0, int(config.get("min_interval_sec", 20)))
        max_concurrency = max(1, int(config.get("max_concurrency", 1)))
        history = [at for at in self.calls[key] if now - at < 86400]
        self.calls[key] = history
        if limit and len(history) >= limit:
            return False, "今天的生图次数已用完，请明天再试。"
        if history and interval and now - history[-1] < interval:
            return False, f"请求太频繁，请 {int(interval - (now - history[-1])) + 1} 秒后再试。"
        if self.inflight[key] >= max_concurrency:
            return False, "上一张图片还在生成，请稍候。"
        self.calls[key].append(now)
        self.inflight[key] += 1
        return True, ""

    def finish(self, key: str) -> None:
        self.inflight[key] = max(0, self.inflight[key] - 1)
