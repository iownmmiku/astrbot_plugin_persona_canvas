"""Photo conditions and visual state lifetimes, independent of chat memory."""
from __future__ import annotations
import copy
import re
import secrets
import time
from .intent import confirmation_text, message_text, state_patch

REQUIREMENT_KEYS = ("outfit", "camera", "pose", "expression", "scene", "avoid", "notes")

def requirements(value):
    if not isinstance(value, dict):
        return {}
    return {key: value[key].strip()[:1000] for key in REQUIREMENT_KEYS if isinstance(value.get(key), str) and value[key].strip()}

def request_text(text):
    return re.sub(r"^/?(?:生图|拍照)\s*", "", message_text(text))

def is_confirmation(text):
    return confirmation_text(request_text(text))

def cancel_request(text):
    clean = re.sub(r'```[\s\S]*?```|[“「『"][\s\S]*?[”」』"]', "", request_text(text)).strip()
    return bool(re.fullmatch(r"(?:请|麻烦|先|那|算了[，,]?\s*)?(?:别拍了|不要拍了|不用拍了|不拍了|别生成了|不要生成了|取消(?:这次|刚才的|本次)?(?:拍摄|生图|生成|照片|任务)|停止(?:拍摄|生图|生成))[。！!，,\s]*", clean))

def pending_request(env, text, reply, required=None, *, kind="photo", ttl=1800):
    return {"request_id": secrets.token_hex(10), "session_key": env["key"], "persona_id": env["persona"]["id"], "request_kind": kind, "request": request_text(text)[:2000], "conditions": reply[:2000], "requirements": {"notes": reply[:1000], **requirements(required)}, "expires_at": time.time() + ttl}

def valid_pending(pending, env, now=None):
    return bool(pending and float(pending.get("expires_at", 0)) > (time.time() if now is None else now) and pending.get("session_key", env["key"]) == env["key"] and pending.get("persona_id", env["persona"]["id"]) == env["persona"]["id"])

def infer_requirements(reply):
    result = {"notes": reply[:1000]}
    if re.search(r"远一点|远景|远些|全身|wide shot|distant", reply, re.I):
        result["camera"] = "远景 / wide shot, camera at a distance"
    elif re.search(r"近一点|近景|close.?up", reply, re.I):
        result["camera"] = "近景 / close-up"
    outfit = re.search(r"(?<!不)(?:穿上|穿着|穿)([^，、。！？?\n]{1,30}?)(?=[，、。！？?\n]|$)", reply)
    if outfit and re.search(r"裙|泳装|泳衣|外套|制服|衬衫|睡衣|帽衫|卫衣|连衣|礼服", outfit[1]):
        result["outfit"] = outfit[1].strip()
    avoid = re.findall(r"(?:不要|不能|不拍|别拍)[^，。！？?\n]{1,40}", reply)
    if avoid:
        result["avoid"] = "；".join(avoid)[:600]
    return result

def apply_state(session, patch, settings, now=None):
    now = time.time() if now is None else now
    times = session.setdefault("state_times", {})
    for key, value in state_patch(patch).items():
        session["state"][key] = value
        ttl = int(settings.get("state_lifetimes", {}).get(key + "_sec", 0))
        times[key] = {"updated_at": now, "expires_at": now + ttl if ttl else 0}

def expire_state(session, persona, settings, now=None):
    now = time.time() if now is None else now
    before = copy.deepcopy(session)
    times = session.setdefault("state_times", {})
    for key, value in session["state"].items():
        ttl = int(settings.get("state_lifetimes", {}).get(key + "_sec", 0))
        if ttl and value != persona.get("state", {}).get(key, "") and key not in times:
            changed_at = float(session.get("updated_at", now))
            times[key] = {"updated_at": changed_at, "expires_at": changed_at + ttl}
        if key in times:
            times[key]["expires_at"] = float(times[key].get("updated_at", now)) + ttl if ttl else 0
        expiry = float(times.get(key, {}).get("expires_at", 0))
        if expiry and expiry <= now:
            session["state"][key] = persona.get("state", {}).get(key, "")
            times.pop(key, None)
    return before != session
