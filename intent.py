from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Intent:
    mode: str = "none"
    use_persona: bool = False
    prompt_delta: str = ""
    scene_prompt: str = ""
    state_patch: dict[str, str] = field(default_factory=dict)
    needs_reference: bool = False
    provider: str = "default"
    caption: str = ""
    raw: str = ""

    @property
    def is_generation(self) -> bool:
        return self.mode != "none"


def _text(value: Any, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _parse_json(text: str) -> dict[str, Any] | None:
    clean = str(text or "").strip()
    clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.I)
    clean = re.sub(r"\s*```$", "", clean)
    try:
        value = json.loads(clean)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        start, end = clean.find("{"), clean.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(clean[start : end + 1])
                return value if isinstance(value, dict) else None
            except (TypeError, ValueError):
                return None
    return None


def validate_intent(value: dict[str, Any] | None, raw: str = "") -> Intent:
    if not value:
        return Intent(raw=raw)
    mode = _text(value.get("mode"), 40)
    aliases = {"selfie": "persona_selfie", "edit": "persona_edit", "normal": "scene", "image": "image_edit"}
    mode = aliases.get(mode, mode)
    if mode not in {"persona_selfie", "persona_edit", "scene", "image_edit", "none"}:
        return Intent(raw=raw)
    patch = value.get("state_patch") if isinstance(value.get("state_patch"), dict) else {}
    state_patch = {key: _text(patch.get(key), 600) for key in ("outfit", "pose", "expression", "scene") if _text(patch.get(key))}
    use_persona = bool(value.get("use_persona", mode.startswith("persona")))
    if mode.startswith("persona"):
        use_persona = True
    if mode == "scene":
        use_persona = False
    scene = _text(value.get("scene_prompt") or value.get("prompt_delta"), 3000)
    return Intent(
        mode=mode,
        use_persona=use_persona,
        prompt_delta=_text(value.get("prompt_delta"), 2000),
        scene_prompt=scene,
        state_patch=state_patch,
        needs_reference=bool(value.get("needs_reference")),
        provider=_text(value.get("provider") or "default", 80),
        caption=_text(value.get("caption"), 300),
        raw=raw,
    )


def heuristic_intent(text: str, has_reference: bool = False) -> Intent:
    raw = _text(text, 4000)
    if not raw:
        return Intent(raw=raw)
    lower = raw.lower()
    persona_words = ("自拍", "你的照片", "你穿", "换衣", "换上", "穿上", "姿势", "摆个", "拍一张你", "人设")
    scene_words = ("大海", "海边", "雪山", "风景", "风光", "一张照片", "画一幅", "生成一张", "画一张", "图片")
    if has_reference and any(word in raw for word in ("改图", "参考图", "按照这张", "把图中", "图生图")):
        return Intent(mode="image_edit", use_persona=False, scene_prompt=raw, needs_reference=True, raw=raw)
    if any(word in raw for word in persona_words):
        patch = {}
        for key, labels in (("outfit", ("换衣", "换上", "穿上")), ("pose", ("姿势", "摆个", "站着", "坐着")), ("scene", ("窗边", "海边", "房间", "户外"))):
            if any(label in raw for label in labels):
                patch[key] = raw
        return Intent(mode="persona_edit" if patch else "persona_selfie", use_persona=True, prompt_delta=raw, state_patch=patch, needs_reference=has_reference, raw=raw)
    if any(word in raw for word in scene_words):
        return Intent(mode="scene", use_persona=False, scene_prompt=raw, raw=raw)
    return Intent(raw=raw)


async def parse_intent(provider: Any, text: str, has_reference: bool = False) -> Intent:
    fallback = heuristic_intent(text, has_reference)
    if provider is None:
        return fallback
    system = (
        "你是生图请求分类器。只返回 JSON，不要 Markdown。判断用户是否要求生成图片。"
        "自拍、换衣、换姿势属于 persona；大海、雪山等独立景物不使用 persona。"
        "字段：mode=persona_selfie|persona_edit|scene|image_edit|none，"
        "use_persona 布尔，prompt_delta 字符串，state_patch 对象(仅 outfit/pose/expression/scene)，"
        "scene_prompt 字符串，needs_reference 布尔，provider 字符串，caption 字符串。"
        "普通聊天必须返回 mode=none。"
    )
    try:
        response = await provider.text_chat(
            prompt=text,
            contexts=[],
            system_prompt=system,
        )
        parsed = _parse_json(getattr(response, "completion_text", ""))
        result = validate_intent(parsed, text)
        return result if result.is_generation or result.mode == "none" and parsed else fallback
    except Exception:
        return fallback


def prompt_bundle(persona: dict[str, Any], intent: Intent, global_negative: str = "") -> tuple[str, str]:
    positive: list[str] = []
    negative: list[str] = []
    if intent.use_persona:
        positive.extend([persona.get("style_prompt", ""), persona.get("positive_prompt", "")])
        state = persona.get("state") or {}
        positive.extend(state.get(key, "") for key in ("outfit", "pose", "expression", "scene"))
        positive.extend(intent.state_patch.values())
        positive.append(intent.prompt_delta)
        negative.extend([persona.get("negative_prompt", ""), global_negative])
    else:
        positive.append(intent.scene_prompt or intent.prompt_delta or intent.raw)
        negative.append(global_negative)
    def clean(values: list[Any], limit: int = 12000) -> str:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            for item in str(value or "").split(","):
                item = item.strip()
                key = item.casefold()
                if item and key not in seen:
                    seen.add(key)
                    result.append(item)
        return ", ".join(result)[:limit]
    return clean(positive), clean(negative, 8000)
