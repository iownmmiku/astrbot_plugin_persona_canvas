from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
OLD_PLUGIN_NAME = "astrbot_plugin_persona_studio"
DEFAULT_PERSONA = {
    "id": "default",
    "name": "默认人设",
    "description": "",
    "positive_prompt": "",
    "negative_prompt": "lowres, worst quality, bad quality, blurry, bad anatomy, bad hands, watermark, logo, signature",
    "style_prompt": "anime illustration, clean lineart, soft shading",
    "state": {"outfit": "", "pose": "", "expression": "", "scene": ""},
    "outfit_pool": ["white shirt and pleated skirt", "casual hoodie", "summer dress"],
    "reference_enabled": False,
    "reference_path": "",
    "created_at": 0,
    "updated_at": 0,
}
DEFAULT_PROVIDER = {
    "name": "default",
    "kind": "openai",
    "endpoint": "",
    "model": "",
    "api_key": "",
    "auth_header": "Authorization",
    "auth_prefix": "Bearer ",
    "extra_body": {},
    "response_path": "",
    "supports_image_edit": False,
    "negative_prompt": True,
}
DEFAULT_SETTINGS = {
    "current_persona": "default",
    "default_provider": "default",
    "llm": {"provider_id": "", "model": "", "fallback_to_current": True, "timeout_sec": 45},
    "providers": {"default": DEFAULT_PROVIDER},
    "moderation": {"enabled": True, "daily_limit": 5, "min_interval_sec": 20, "max_concurrency": 1},
    "active": {"enabled": False, "check_interval_sec": 30, "default_start": "09:00", "default_end": "22:00", "min_gap_sec": 3600, "silence_after": 3, "silence_hours": 24},
    "good_morning": {"enabled": False, "start": "07:00", "end": "10:00", "timezone": "Asia/Shanghai"},
    "generation": {"width": 832, "height": 1216, "steps": 28, "scale": 5, "sampler": "k_euler_ancestral", "seed": -1, "max_history": 100},
}


def _data_dir() -> Path:
    try:
        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir("astrbot_plugin_persona_canvas"))
    except Exception:
        return Path(__file__).resolve().parent / "data"


def _legacy_data_dir() -> Path | None:
    try:
        from astrbot.api.star import StarTools

        return Path(StarTools.get_data_dir(OLD_PLUGIN_NAME))
    except Exception:
        return Path(__file__).resolve().parent.parent / OLD_PLUGIN_NAME / "data"


def _merge(base: dict[str, Any], incoming: Any) -> dict[str, Any]:
    out = copy.deepcopy(base)
    if not isinstance(incoming, dict):
        return out
    for key, value in incoming.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _safe_text(value: Any, limit: int = 8000) -> str:
    return str(value or "").strip()[:limit]


class Storage:
    def __init__(self, directory: Path | None = None):
        self.root = directory or _data_dir()
        self.assets = self.root / "assets"
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_data()
        self.personas_path = self.root / "personas.json"
        self.settings_path = self.root / "settings.json"
        self.targets_path = self.root / "targets.json"
        self.history_path = self.root / "history.jsonl"
        self.runtime_path = self.root / "runtime.json"
        self.personas = self._load(self.personas_path, {"version": SCHEMA_VERSION, "items": [copy.deepcopy(DEFAULT_PERSONA)]})
        self.settings = _merge(DEFAULT_SETTINGS, self._load(self.settings_path, {}))
        self.targets = self._load(self.targets_path, {"version": SCHEMA_VERSION, "items": []})
        self.runtime = self._load(self.runtime_path, {"version": SCHEMA_VERSION, "morning": {}})
        self._normalise()

    def _migrate_legacy_data(self) -> None:
        """Copy old Persona Studio data without deleting the legacy directory."""
        marker = self.root / ".migrated_from_persona_studio"
        if marker.exists():
            return
        data_files = ("personas.json", "settings.json", "targets.json", "history.jsonl", "runtime.json", "webui_token.txt")
        if any((self.root / name).exists() for name in data_files):
            return
        legacy = _legacy_data_dir()
        if not legacy or legacy.resolve() == self.root.resolve() or not legacy.is_dir():
            return
        copied: list[str] = []
        for name in ("personas.json", "settings.json", "targets.json", "history.jsonl", "runtime.json", "webui_token.txt"):
            source = legacy / name
            target = self.root / name
            if source.is_file():
                shutil.copy2(source, target)
                copied.append(name)
        source_assets = legacy / "assets"
        if source_assets.is_dir():
            self.assets.mkdir(parents=True, exist_ok=True)
            for source in source_assets.iterdir():
                if source.is_file():
                    shutil.copy2(source, self.assets / source.name)
            copied.append("assets")
        marker.write_text(json.dumps({"migrated_from": str(legacy), "copied": copied, "at": time.time()}, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else copy.deepcopy(default)
        except (OSError, ValueError, TypeError):
            return copy.deepcopy(default)

    @staticmethod
    def _atomic_write(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _normalise(self) -> None:
        items = self.personas.get("items")
        if not isinstance(items, list) or not items:
            self.personas["items"] = [copy.deepcopy(DEFAULT_PERSONA)]
        self.personas["items"] = [_merge(DEFAULT_PERSONA, item) for item in self.personas["items"] if isinstance(item, dict)]
        if not any(item["id"] == "default" for item in self.personas["items"]):
            self.personas["items"].insert(0, copy.deepcopy(DEFAULT_PERSONA))
        self.personas["version"] = SCHEMA_VERSION
        self.targets["items"] = [item for item in self.targets.get("items", []) if isinstance(item, dict) and item.get("umo")]
        self.targets["version"] = SCHEMA_VERSION
        self.runtime["version"] = SCHEMA_VERSION
        self.save_all()

    def save_all(self) -> None:
        self._atomic_write(self.personas_path, self.personas)
        self._atomic_write(self.settings_path, self.settings)
        self._atomic_write(self.targets_path, self.targets)
        self._atomic_write(self.runtime_path, self.runtime)

    def save_personas(self) -> None:
        self._atomic_write(self.personas_path, self.personas)

    def save_settings(self) -> None:
        self._atomic_write(self.settings_path, self.settings)

    def save_targets(self) -> None:
        self._atomic_write(self.targets_path, self.targets)

    def persona(self, persona_id: str | None = None) -> dict[str, Any]:
        wanted = persona_id or self.settings.get("current_persona", "default")
        for item in self.personas["items"]:
            if item.get("id") == wanted:
                return item
        return self.personas["items"][0]

    def upsert_persona(self, value: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        item = _merge(DEFAULT_PERSONA, value)
        item["id"] = _safe_text(item.get("id") or secrets.token_hex(8), 80)
        item["name"] = _safe_text(item.get("name") or "未命名人设", 100)
        item["updated_at"] = now
        item["created_at"] = item.get("created_at") or now
        item["state"] = _merge(DEFAULT_PERSONA["state"], item.get("state"))
        item["outfit_pool"] = [str(x)[:500] for x in item.get("outfit_pool", []) if str(x).strip()][:50]
        for index, old in enumerate(self.personas["items"]):
            if old.get("id") == item["id"]:
                self.personas["items"][index] = item
                break
        else:
            self.personas["items"].append(item)
        self.save_personas()
        return item

    def append_history(self, item: dict[str, Any]) -> None:
        safe = {"at": time.time(), **item}
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False) + "\n")

    def recent_history(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()[-max(1, limit):]
            return [json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError):
            return []

    def save_asset(self, data: bytes, suffix: str = "png") -> Path:
        digest = hashlib.sha256(data).hexdigest()[:24]
        path = self.assets / f"{digest}.{suffix.lstrip('.') or 'bin'}"
        if not path.exists():
            path.write_bytes(data)
        return path

    def webui_token(self, configured: str = "") -> str:
        if configured.strip():
            return configured.strip()
        path = self.root / "webui_token.txt"
        try:
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        except OSError:
            pass
        token = secrets.token_urlsafe(32)
        path.write_text(token + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return token
