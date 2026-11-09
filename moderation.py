from __future__ import annotations

import re


class Moderation:
    """Content check plus quota reservations persisted by Storage."""

    def __init__(self, settings, storage):
        self.settings, self.storage = settings, storage

    def check_content(self, text):
        if re.search(r"未成年.{0,8}(?:色情|裸体|性行为)|儿童.{0,8}(?:色情|裸体|性行为)", text):
            raise ValueError("该请求不允许生成")

    def reserve(self, key, text, *, is_admin=False):
        """Reserve one generation; retain its token until completion."""
        try:
            self.check_content(text)
        except ValueError as exc:
            return False, str(exc), None
        return self.storage.reserve_quota_token(key, self.settings.get("moderation", {}), is_admin=is_admin)

    def finish(self, key, *, success=True, token=None):
        # The key is kept for call-site readability; the token identifies the row.
        self.storage.finish_quota_token(token, success=success)
