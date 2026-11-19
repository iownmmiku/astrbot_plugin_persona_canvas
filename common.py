"""Small helpers shared by the runtime modules: time zones, redaction, states."""

from __future__ import annotations

import math
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

# Aspect ratios the role may choose (the set Gemini image models accept).
ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
DEFAULT_ASPECT_RATIO = "2:3"
# Resolution tiers: about edge × edge pixels, whatever the aspect ratio.
RESOLUTIONS = {"1K": 1024, "2K": 2048, "4K": 4096}
DEFAULT_RESOLUTION = "1K"


def aspect_ratio(value: Any) -> str:
    """Normalise a chosen ratio such as ``16:9`` or ``3：4`` to a supported one.

    Other numeric ratios snap to the closest supported shape; unusable input
    returns an empty string so the caller can apply the default.
    """
    text = re.sub(r"\s+", "", str(value or "")).translate(
        str.maketrans({"：": ":", "x": ":", "X": ":", "×": ":", "/": ":"})
    )
    if text in ASPECT_RATIOS:
        return text
    match = re.fullmatch(r"(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)", text)
    if not match or not float(match[1]) or not float(match[2]):
        return ""
    target = float(match[1]) / float(match[2])
    return min(ASPECT_RATIOS, key=lambda item: abs(math.log(_ratio_value(item) / target)))


def _ratio_value(ratio: str) -> float:
    width, height = ratio.split(":")
    return int(width) / int(height)


def canvas_size(ratio: str, resolution: str) -> tuple[int, int]:
    """Pixel size for a ratio and tier: within edge² pixels, sides multiples of
    64 and at most 4096 (1K 2:3 → 832×1216, 1K 16:9 → 1344×768)."""
    edge = RESOLUTIONS.get(resolution, RESOLUTIONS[DEFAULT_RESOLUTION])
    value = _ratio_value(ratio if ratio in ASPECT_RATIOS else DEFAULT_ASPECT_RATIO)
    width, height = edge * math.sqrt(value), edge / math.sqrt(value)
    scale = min(1.0, 4096 / max(width, height))
    # Round first so 3072.0 computed as 3071.9999 does not drop a 64-step.
    return tuple(max(64, int(round(side * scale, 6)) // 64 * 64) for side in (width, height))


def apply_canvas(options: dict, chosen: str = "") -> dict:
    """Fill ``aspect_ratio``, ``width``/``height`` and ``image_size`` in place.

    Values set by hand in the workbench (listed in ``_explicit``) win. Otherwise
    the role's ratio, or the default portrait 2:3, and the configured tier
    decide. Adapters use what they support: pixel sizes, or ratio plus tier.
    """
    explicit = set(options.get("_explicit") or [])
    tier = str(options.get("resolution") or "")
    tier = tier if tier in RESOLUTIONS else DEFAULT_RESOLUTION
    options["resolution"] = tier
    manual = aspect_ratio(options.get("aspect_ratio")) if "aspect_ratio" in explicit else ""
    ratio = manual or aspect_ratio(chosen) or DEFAULT_ASPECT_RATIO
    if "aspect_ratio" not in explicit:
        options["aspect_ratio"] = ratio
    sized = {"width", "height", "size"} & explicit
    if not sized:
        options["width"], options["height"] = canvas_size(ratio, tier)
    elif sized == {"width"}:
        width = int(options["width"])
        options["height"] = max(64, min(4096, round(width / _ratio_value(ratio) / 64) * 64))
    elif sized == {"height"}:
        height = int(options["height"])
        options["width"] = max(64, min(4096, round(height * _ratio_value(ratio) / 64) * 64))
    if tier != DEFAULT_RESOLUTION and "image_size" not in explicit:
        # Only sent for 2K/4K: 1K is the default and older models reject the field.
        options["image_size"] = tier
    return options


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
