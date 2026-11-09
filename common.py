"""Small helpers shared by the runtime modules: time zones, redaction, states."""

from __future__ import annotations

import re
from datetime import timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Jobs in these states may still be withdrawn before they are sent.
CANCELLABLE_JOB_STATUSES = frozenset({"queued", "deciding", "generating", "succeeded"})
# Role decisions that create an image, mapped to the generation mode.
DECISION_MODES = {"photo": "persona", "scene": "scene", "edit": "edit"}
MODE_DECISIONS = {mode: decision for decision, mode in DECISION_MODES.items()}

# Fixed-offset zones without daylight saving time. Windows Python often lacks
# the IANA database; these keep the default schedules working without tzdata.
_FALLBACK_OFFSETS = {
    "Asia/Shanghai": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Macau": 8,
    "Asia/Taipei": 8,
    "Asia/Singapore": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "UTC": 0,
    "Etc/UTC": 0,
}


SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_token",
        "authorization",
        "auth",
        "auth_key",
        "auth_token",
        "auth_value",
        "access_token",
        "refresh_token",
        "bearer_token",
        "token",
        "secret",
        "client_secret",
        "password",
        "x_api_key",
    }
)


def secret_key(key: Any) -> bool:
    """Whether a settings/workflow field name holds a credential."""
    return str(key).lower().replace("-", "_") in SECRET_KEYS


class UnknownTimezoneError(ZoneInfoNotFoundError):
    """Raised for a zone unavailable here; still a ZoneInfoNotFoundError."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "该时区在当前 Python 环境不可用"


@lru_cache(maxsize=64)
def zone(name: str) -> tzinfo:
    """Return the IANA zone, or a fixed-offset fallback when tzdata is absent."""
    key = str(name or "").strip()
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        hours = _FALLBACK_OFFSETS.get(key)
        if hours is None:
            raise UnknownTimezoneError(f"时区 {key or '（空）'} 在当前 Python 环境不可用") from exc
        return timezone(timedelta(hours=hours), key)


_BEARER = re.compile(r"(?i)(bearer\s+)[^\s,\"']+")
_QUERY_SECRET = re.compile(r"(?i)([?&](?:key|api_key|token|access_token)=)[^&\s]+")


def redact_text(storage: Any, value: Any, *, limit: int | None = 500, patterns: bool = True) -> str:
    """Hide configured credentials and bearer/query tokens in free text.

    Known secrets anywhere in the settings are replaced first; provider keys are
    replaced regardless of length. Error text also masks generic token
    patterns; prompts pass ``patterns=False`` so wording like "bearer of" stays.
    """
    text = str(value)
    scrub = getattr(storage, "_scrub_known_secrets", None)
    if callable(scrub):
        text = str(scrub(text))
    for config in (getattr(storage, "settings", None) or {}).get("providers", {}).values():
        if isinstance(config, dict):
            for key in ("api_key", "auth_value"):
                secret = str(config.get(key) or "")
                if secret:
                    text = text.replace(secret, "[已隐藏]")
    if patterns:
        text = _QUERY_SECRET.sub(r"\1[已隐藏]", _BEARER.sub(r"\1[已隐藏]", text))
    return text if limit is None else text[:limit]
