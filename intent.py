"""Strict request eligibility; only a role decision may authorize generation."""
from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

STATE_KEYS = ("outfit", "pose", "expression", "scene")

@dataclass
class Intent:
    mode: str = "none"
    use_persona: bool = False
    prompt_delta: str = ""
    scene_prompt: str = ""
    state_patch: dict[str, str] = field(default_factory=dict)
    needs_reference: bool = False
    provider: str = ""
    caption: str = ""
    raw: str = ""
    @property
    def is_generation(self) -> bool:
        return self.mode in {"persona_selfie", "persona_edit", "scene", "image_edit"}

def parse_json(text: str) -> dict:
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text).strip(), flags=re.I)
    try:
        value = json.loads(clean)
    except ValueError:
        raise ValueError("角色没有返回有效的决策 JSON，请重试或检查聊天模型") from None
    if not isinstance(value, dict):
        raise ValueError("角色决策必须是对象")
    return value

def state_patch(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {key: str(value[key]).strip()[:600] for key in STATE_KEYS if key in value and isinstance(value[key], str)}

def photo_request(text: str, pending: dict | None = None) -> bool:
    text = str(text).strip()
    if not text:
        return False
    clean = re.sub(r'```[\s\S]*?```|[“「『"][\s\S]*?[”」』"]', "", text).strip()
    if re.search(r"(?:讲解|讲讲|解释|教程|教学|科普|原理|过程|习惯|为什么|如何使用|怎么使用)", clean):
        return False
    if re.search(r"(?:不要|别再?|不用|不想|不需要|不许|禁止)[^，。！？,!?.\n]{0,16}(?:拍|画|生成|发图|照片|图片|自拍)", clean):
        return False
    if re.search(r"(?:昨天|昨晚|之前|刚才|曾经|已经).{0,10}(?:我|他|她|我们)?(?:拍|画|生成)了?", clean) and not re.search(r"(?:给我|帮我|再拍|再画|发我)", clean):
        return False
    if re.search(r"(?:假如|假设|想象一下|比如|举例|引用|讨论).{0,30}(?:拍|画|照片|图片)", clean):
        return False
    if re.search(r"(?:给我|帮我|发我|让我看看|我想看|我想要).{0,30}(?:拍|画|生成|照片|自拍|图片)", clean):
        return True
    if re.search(r"(?:能不能|可以|能|愿意).{0,12}(?:拍|画|生成|发).{0,12}(?:张|幅|照片|图片|自拍|图)", clean):
        return True
    if re.search(r"(?:^|[，。！？,!?:：])\s*(?:请|麻烦|帮我|给我|你|再|就这样)?\s*(?:拍|画|生成|发)(?:一|两|几|张|个|幅|下|只|条|座|片)", clean):
        return True
    if re.search(r"(?:把|将).{0,20}(?:这张|那张|刚才的|上张|图片|照片|图).{0,25}(?:改|换|编辑|重绘)", clean):
        return True
    if re.search(r"(?:再来|再拍|再画).{0,8}(?:张|幅|次)", clean):
        return bool(pending and (pending.get("last_image") or pending.get("request")))
    if re.fullmatch(r"(?:请|再|那就|你)?拍(?:近|远)一?[点些](?:吧|好吗)?[。！!，,\s]*", clean):
        return True
    if pending and pending.get("request_kind") == "photo" and float(pending.get("expires_at", 0)) > time.time():
        if re.fullmatch(r"(?:那|那就|请)?(?:镜头|构图)(?:再)?(?:近|远)一?[点些](?:吧|好吗)?[。！!，,\s]*", clean):
            return True
        return bool(re.fullmatch(r"(?:好[的啊呀]?|可以|行|嗯|同意|就这样|按你说的|就按你说的拍|好，就按你说的拍)[。！!，,\s]*", clean))
    return False

def state_request(text: str, pending: dict | None = None) -> bool:
    clean = re.sub(r'```[\s\S]*?```|[“「『"][\s\S]*?[”」』"]', "", text).strip()
    if re.search(r"(?:假如|假设|想象|比如|引用|讨论|她说|他说|我说|我正|我坐|我站|我穿|昨天|刚才).{0,20}(?:换|穿|坐|站|摆)", clean) or clean.startswith(("我坐", "我站", "我穿")):
        return False
    text = clean
    if re.search(r"(?:不要|别|不想|不用).{0,12}(?:换|穿|摆|坐|站)", text):
        return False
    if pending and pending.get("request_kind") == "state" and float(pending.get("expires_at", 0)) > time.time():
        if re.fullmatch(r"(?:好[的啊呀]?|可以|行|嗯|同意|就这样|按你说的)[。！!，,\s]*", text.strip()):
            return True
    return bool(re.search(r"(?:换成|换上|穿上|换衣|换个姿势|摆个|坐在|站在|笑一个)", text))

def validate_intent(value: dict | None, raw: str = "") -> Intent:
    value = value or {}
    mode = {"photo": "persona_selfie", "selfie": "persona_selfie", "edit": "image_edit", "normal": "scene"}.get(str(value.get("mode")), str(value.get("mode", "none")))
    if mode not in {"persona_selfie", "persona_edit", "scene", "image_edit", "none"}:
        mode = "none"
    return Intent(mode=mode, use_persona=mode.startswith("persona"), prompt_delta=str(value.get("prompt_delta") or value.get("prompt") or "")[:5000], scene_prompt=str(value.get("scene_prompt") or value.get("prompt") or "")[:5000], state_patch=state_patch(value.get("state_patch")), needs_reference=mode == "image_edit" or bool(value.get("needs_reference")), provider=str(value.get("provider") or "")[:80], caption=str(value.get("caption") or value.get("reply") or "")[:1000], raw=raw)

def heuristic_intent(text: str, has_reference: bool = False) -> Intent:
    # Classification never grants role consent.
    if not photo_request(text):
        return Intent(raw=text)
    if has_reference and re.search(r"改|换|编辑|重绘", text):
        return Intent(mode="image_edit", needs_reference=True, scene_prompt=text, raw=text)
    persona = bool(re.search(r"自拍|你的照片|拍.{0,10}你|你.{0,6}穿|人设", text))
    return Intent(mode="persona_selfie" if persona else "scene", use_persona=persona, prompt_delta=text if persona else "", scene_prompt="" if persona else text, raw=text)

def prompt_bundle(persona: dict, intent: Intent, global_negative: str = "") -> tuple[str, str]:
    if intent.use_persona:
        state = {**(persona.get("state") or {}), **intent.state_patch}
        positive = [persona.get("style_prompt", ""), persona.get("positive_prompt", ""), *(state.get(key, "") for key in STATE_KEYS), intent.prompt_delta]
        negative = [persona.get("negative_prompt", ""), global_negative]
    else:
        positive, negative = [intent.scene_prompt or intent.prompt_delta or intent.raw], [global_negative]
    return ", ".join(str(x).strip() for x in positive if str(x).strip())[:12000], ", ".join(str(x).strip() for x in negative if str(x).strip())[:8000]

DECISION_RULES = """
你仍然是原人格；下面只是拍摄工具的行为约定。照片请求必须先经过你的真实意愿决定。
可以害羞、拒绝、询问或提出条件，不要为了调用工具而默认答应。反应由人格和当前对话决定，不使用随机拒绝。
提及照片、讨论、假设、引用、否定都不是拍摄指令；普通聊天不要调用生图工具。
提出条件并询问用户是否同意时，本轮不能拍照；等用户明确回应后再决定。
只有你愿意且用户明确要照片/画图或已确认拍摄条件时才调用 persona_canvas_photo。
固定长相不可被临时衣服/场景覆盖；害羞、姿势和镜头选择要落实进照片描述。
单独换装可以调用 persona_canvas_state，不会自动拍照。图片编辑必须提供实际参考图。
提拍摄条件时使用 persona_canvas_conditions 记录具体衣服、镜头和禁止内容。待确认要求被修改后重新询问，不得直接拍。
photo_records 是实际拍摄记录：queued/generating 仅为正在生成，succeeded 仅表示生成完成，sent 才表示已发图，cancelled 不得继续发送；以记录为准，不声称看过未生成的画面。
记录中的 summary 和 visual_state 是生成时的描述与状态快照，不是视觉模型对图片的识别结论。mode 为 scene 时是普通绘图，不是你的自拍。
一次请求只拍一次。工具返回失败时如实说明，不能声称图片已完成。工具已发送图片时简短回应即可。
工具返回后不要再次调用拍摄工具或把失败当成新的请求。不要向用户展示内部 JSON 或配置。
"""

def validated_decision(text: str) -> dict:
    value = parse_json(text)
    action = str(value.get("decision") or value.get("action") or "")
    if action not in {"photo", "scene", "edit", "state", "ask", "refuse", "chat", "skip"}:
        raise ValueError("角色决策类型无效")
    reply = str(value.get("reply") or "").strip()[:1000]
    prompt = str(value.get("prompt") or "").strip()[:5000]
    if action in {"photo", "scene", "edit"} and not prompt:
        raise ValueError("角色同意拍摄但没有给出完整描述")
    if action != "skip" and not reply:
        raise ValueError("角色决策缺少回复")
    from .companion import requirements
    return {"decision": action, "reply": reply, "prompt": prompt, "state_patch": state_patch(value.get("state_patch")), "requirements": requirements(value.get("requirements")), "needs_reference": action == "edit" or bool(value.get("needs_reference"))}
