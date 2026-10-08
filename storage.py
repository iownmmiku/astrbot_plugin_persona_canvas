from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 5
OLD_PLUGIN_NAME = "astrbot_plugin_persona_studio"
DEFAULT_PERSONA = {
    "id": "default", "name": "默认人设", "description": "",
    "astrbot_persona_id": "", "consent_prompt": "", "positive_prompt": "",
    "negative_prompt": "lowres, worst quality, bad quality, blurry, bad anatomy, bad hands, watermark, logo, signature",
    "style_prompt": "anime illustration, clean lineart, soft shading",
    "state": {"outfit": "", "pose": "", "expression": "", "scene": ""},
    "outfit_pool": ["white shirt and pleated skirt", "casual hoodie", "summer dress"],
    "reference_enabled": False, "reference_asset": "", "reference_assets": [], "reference_path": "",
    "created_at": 0, "updated_at": 0,
}
DEFAULT_PROVIDER = {
    "name": "default", "kind": "openai", "endpoint": "", "model": "", "api_key": "",
    "auth_header": "Authorization", "auth_prefix": "Bearer ", "extra_body": {},
    "response_path": "", "supports_image_edit": False, "negative_prompt": True,
}
DEFAULT_SETTINGS = {
    "current_persona": "default", "default_provider": "default",
    "llm": {"provider_id": "", "model": "", "fallback_to_current": True, "timeout_sec": 45},
    "integration": {"enabled": True, "mode": "native_tools", "strict_trigger": True},
    "dialogue": {"enabled": True, "timeout_sec": 60, "context_turns": 12, "confirmation_ttl_sec": 1800},
    "state_lifetimes": {"outfit_sec": 0, "pose_sec": 1800, "expression_sec": 1800, "scene_sec": 14400},
    "proactive_budget": {"messages": 10, "photos": 2, "llm": 24, "timezone": "Asia/Shanghai"},
    "providers": {"default": DEFAULT_PROVIDER},
    "moderation": {"enabled": True, "daily_limit": 5, "min_interval_sec": 20, "max_concurrency": 1, "timezone": "Asia/Shanghai"},
    "active": {"enabled": False, "check_interval_sec": 30, "default_start": "09:00", "default_end": "22:00", "min_gap_sec": 3600, "min_idle_sec": 1800, "timezone": "Asia/Shanghai", "silence_after": 3, "silence_hours": 24, "jitter_percent": 15},
    "good_morning": {"enabled": False, "start": "07:00", "end": "10:00", "timezone": "Asia/Shanghai"},
    "generation": {"width": 832, "height": 1216, "steps": 28, "scale": 5, "sampler": "k_euler_ancestral", "seed": -1, "max_history": 100, "max_concurrency": 2, "timeout_sec": 180},
}
JOB_STATUSES = frozenset({"queued", "deciding", "generating", "succeeded", "failed", "sending", "sent", "uncertain", "cancelled"})
DELIVERY_STATUSES = frozenset({"claimed", "decided", "skipped", "generating", "sending", "sent", "failed", "uncertain"})
_LEGACY_FILES = ("personas.json", "settings.json", "targets.json", "history.jsonl", "runtime.json", "webui_token.txt")
_SECRET_KEYS = frozenset({"api_key", "apikey", "api_token", "authorization", "auth", "auth_key", "auth_token", "auth_value", "access_token", "refresh_token", "bearer_token", "token", "secret", "client_secret", "password", "x_api_key"})
_ASSET_SUFFIXES = frozenset({"png", "jpg", "jpeg", "webp", "gif", "avif", "bin"})


class StorageError(RuntimeError):
    """A preserved data/configuration error requiring explicit recovery."""


class StorageConflictError(StorageError):
    """The session changed after the caller read its revision."""


def _data_dir() -> Path:
    try:
        from astrbot.api.star import StarTools
        return Path(StarTools.get_data_dir("astrbot_plugin_persona_canvas"))
    except (ImportError, AttributeError):
        return Path(__file__).resolve().parent / "data"


def _legacy_data_dir() -> Path | None:
    try:
        from astrbot.api.star import StarTools
        return Path(StarTools.get_data_dir(OLD_PLUGIN_NAME))
    except (ImportError, AttributeError):
        return Path(__file__).resolve().parent.parent / OLD_PLUGIN_NAME / "data"


def _merge(base: dict[str, Any], incoming: Any) -> dict[str, Any]:
    result = copy.deepcopy(base)
    if isinstance(incoming, dict):
        for key, value in incoming.items():
            result[key] = _merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else copy.deepcopy(value)
    return result


def _safe_text(value: Any, limit: int = 8000) -> str:
    return str(value or "").strip()[:limit]


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise StorageError("数据包含不能保存的值") from exc


def _decoded(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise StorageError(f"{label} 数据损坏；原数据已保留，请先恢复备份") from exc
    if not isinstance(value, dict):
        raise StorageError(f"{label} 必须是 JSON 对象；原数据已保留")
    return value


def _secret_key(key: str) -> bool:
    return str(key).lower().replace("-", "_") in _SECRET_KEYS


def _redacted(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: _redacted(item) for key, item in value.items() if not _secret_key(key)}
        for key, item in value.items():
            if _secret_key(key):
                result[f"has_{str(key).lower().replace('-', '_')}"] = bool(item)
        return result
    return [_redacted(item) for item in value] if isinstance(value, list) else copy.deepcopy(value)


class Storage:
    """SQLite is authoritative; save methods persist mutable document facades.

    Session/job/delivery methods return detached snapshots. Passing a directory
    confines JSON migration to it, so development never scans live plugin data.
    """

    def __init__(self, directory: Path | None = None):
        self.root = (Path(directory) if directory is not None else _data_dir()).resolve()
        self.assets = self.root / "assets"
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "persona_canvas.sqlite3"
        # Display compatibility only; new data is never written to these files.
        for name in ("personas", "settings", "targets", "runtime"):
            setattr(self, f"{name}_path", self.root / f"{name}.json")
        self.history_path = self.root / "history.jsonl"
        self._lock, self._closed = threading.RLock(), False
        try:
            self._db = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA busy_timeout=30000")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StorageError("数据库来自更新版本的插件，请升级插件后再打开")
            if version and version < SCHEMA_VERSION:
                self._backup_database(version)
            self._create_schema()
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            if not self._db.execute("SELECT 1 FROM documents LIMIT 1").fetchone():
                source = self.root
                if directory is None and not any((self.root / name).exists() for name in _LEGACY_FILES):
                    legacy = _legacy_data_dir()
                    if legacy is not None and legacy.is_dir() and legacy.resolve() != self.root:
                        source = legacy
                self._initialise_documents(source)
            self.personas = self._read_document("personas")
            self.settings = _merge(DEFAULT_SETTINGS, self._read_document("settings"))
            self.targets, self.runtime = self._read_document("targets"), self._read_document("runtime")
            self._normalise()
            with self._db:
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            try:
                self.db_path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            if hasattr(self, "_db"):
                self._db.close()
            self._closed = True
            raise

    def _create_schema(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (name TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (umo TEXT PRIMARY KEY, updated_at REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, at REAL NOT NULL, updated_at REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_time ON jobs(at DESC);
            CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, status TEXT NOT NULL, at REAL NOT NULL, updated_at REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quota_usage (user_key TEXT NOT NULL, day TEXT NOT NULL, successes INTEGER NOT NULL DEFAULT 0, last_attempt REAL NOT NULL DEFAULT 0, PRIMARY KEY(user_key, day));
            CREATE TABLE IF NOT EXISTS quota_reservations (id INTEGER PRIMARY KEY AUTOINCREMENT, user_key TEXT NOT NULL, day TEXT NOT NULL, at REAL NOT NULL, expires_at REAL NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS quota_user ON quota_reservations(user_key, at);
            CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, session_key TEXT NOT NULL, at REAL NOT NULL, updated_at REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS actions_session ON actions(session_key,at DESC);
            CREATE TABLE IF NOT EXISTS budget_usage (kind TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,day));
            CREATE TABLE IF NOT EXISTS budget_reservations (id TEXT PRIMARY KEY, kind TEXT NOT NULL, day TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS asset_leases (id TEXT PRIMARY KEY, name TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS asset_lease_expiry ON asset_leases(expires_at);
        """)

    def _backup_database(self, version: int) -> None:
        directory = self.root / "migration_backups"
        directory.mkdir(parents=True, exist_ok=True)
        backup = sqlite3.connect(str(directory / f"sqlite-v{version}-{time.time_ns()}.sqlite3"))
        try:
            self._db.backup(backup)
        finally:
            backup.close()

    def _initialise_documents(self, source: Path) -> None:
        documents = {
            "personas": {"version": SCHEMA_VERSION, "items": [copy.deepcopy(DEFAULT_PERSONA)]},
            "settings": copy.deepcopy(DEFAULT_SETTINGS),
            "targets": {"version": SCHEMA_VERSION, "items": []},
            "runtime": {"version": SCHEMA_VERSION, "morning": {}},
        }
        existing = [source / name for name in _LEGACY_FILES if (source / name).is_file()]
        if existing:
            backup = self.root / "migration_backups" / f"legacy-json-{time.time_ns()}"
            backup.mkdir(parents=True, exist_ok=False)
            for path in existing:
                shutil.copy2(path, backup / path.name)
        for name in documents:
            path = source / f"{name}.json"
            if path.is_file():
                try:
                    documents[name] = _decoded(path.read_text(encoding="utf-8-sig"), path.name)
                except (OSError, UnicodeError) as exc:
                    raise StorageError(f"无法读取 {path.name}；原文件已保留") from exc
        history = []
        if (source / "history.jsonl").is_file():
            try:
                with (source / "history.jsonl").open(encoding="utf-8-sig") as handle:
                    for number, line in enumerate(handle, 1):
                        if line.strip():
                            history.append(_decoded(line, f"history.jsonl 第 {number} 行"))
            except (OSError, UnicodeError) as exc:
                raise StorageError("无法读取生成历史；原文件已保留") from exc
        # Validate before committing. Broken data never becomes default records.
        documents["personas"] = self._normalised_personas(documents["personas"])
        documents["targets"] = self._normalised_targets(documents["targets"])
        documents["settings"] = _merge(DEFAULT_SETTINGS, documents["settings"])
        self._validate_settings(documents["settings"])
        if "auto_route" in documents["settings"]:
            documents["settings"]["integration"]["enabled"] = bool(documents["settings"].pop("auto_route"))
        source_assets = source / "assets"
        if source.resolve() != self.root and source_assets.is_dir():
            for path in source_assets.iterdir():
                if path.is_file() and not path.is_symlink() and not (self.assets / path.name).exists():
                    shutil.copy2(path, self.assets / path.name)
        for persona in documents["personas"]["items"]:
            reference = str(persona.get("reference_path") or "")
            if not persona.get("reference_asset") and reference:
                name = reference.replace("\\", "/").rsplit("/", 1)[-1]
                if (self.assets / name).is_file():
                    persona["reference_asset"] = name
        token = source / "webui_token.txt"
        if source.resolve() != self.root and token.is_file() and not (self.root / token.name).exists():
            shutil.copy2(token, self.root / token.name)
        self.settings = documents["settings"]
        with self._db:
            for name, value in documents.items():
                self._write_document(name, value)
            for item in history:
                self._db.execute("INSERT INTO history(at,payload) VALUES(?,?)", (self._timestamp(item.get("at")), _json(self._scrub_known_secrets(item))))

    @staticmethod
    def _timestamp(value: Any) -> float:
        try:
            result = float(value or 0)
            if result != result or abs(result) == float("inf"):
                raise ValueError()
            return result
        except (TypeError, ValueError) as exc:
            raise StorageError("时间戳无效；原数据已保留") from exc

    def _check_open(self) -> None:
        if self._closed:
            raise StorageError("数据库已关闭")

    def _write_document(self, name: str, value: dict[str, Any]) -> None:
        self._db.execute("INSERT INTO documents(name,payload) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload", (name, _json(value)))

    def _read_document(self, name: str) -> dict[str, Any]:
        row = self._db.execute("SELECT payload FROM documents WHERE name=?", (name,)).fetchone()
        if row is None:
            raise StorageError(f"数据库缺少 {name}；请恢复备份")
        return _decoded(row["payload"], name)

    @staticmethod
    def _normalised_personas(value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value.get("items"), list):
            raise StorageError("人设列表格式无效；原数据已保留")
        result, seen = copy.deepcopy(value), set()
        result["items"] = []
        for number, item in enumerate(value["items"]):
            if not isinstance(item, dict):
                raise StorageError("人设记录格式无效；原数据已保留")
            merged = _merge(DEFAULT_PERSONA, item)
            merged["id"] = _safe_text(item.get("id") or f"legacy-{number + 1}", 80)
            if merged["id"] in seen:
                raise StorageError("人设 ID 重复；原数据已保留")
            seen.add(merged["id"])
            if not isinstance(merged.get("state"), dict) or not isinstance(merged.get("outfit_pool"), list):
                raise StorageError("人设状态或服装池格式无效；原数据已保留")
            refs = merged.get("reference_assets")
            if not isinstance(refs, list) or len(refs) > 8 or any(not isinstance(name, str) for name in refs):
                raise StorageError("人设参考图库格式无效；原数据已保留")
            result["items"].append(merged)
        if "default" not in seen:
            result["items"].insert(0, copy.deepcopy(DEFAULT_PERSONA))
        result["version"] = SCHEMA_VERSION
        return result

    @staticmethod
    def _normalised_targets(value: dict[str, Any]) -> dict[str, Any]:
        items = value.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) or not item.get("umo") for item in items):
            raise StorageError("主动消息目标格式无效；原数据已保留")
        result = copy.deepcopy(value)
        result["version"] = SCHEMA_VERSION
        return result

    @staticmethod
    def _validate_settings(value: dict[str, Any]) -> None:
        for key in ("providers", "llm", "integration", "dialogue", "moderation", "active", "good_morning", "generation", "state_lifetimes", "proactive_budget"):
            if not isinstance(value.get(key), dict):
                raise StorageError(f"设置中的 {key} 格式无效；原数据已保留")
        if any(not isinstance(provider, dict) for provider in value["providers"].values()):
            raise StorageError("Provider 配置格式无效；原数据已保留")

    def _normalise(self) -> None:
        self.personas = self._normalised_personas(self.personas)
        self.targets = self._normalised_targets(self.targets)
        self.runtime["version"] = SCHEMA_VERSION
        self._validate_settings(self.settings)
        if self.settings.get("current_persona") not in {item["id"] for item in self.personas["items"]}:
            self.settings["current_persona"] = "default"
        self.save_all()

    def save_all(self) -> None:
        with self._lock:
            self._check_open()
            self._validate_settings(self.settings)
            # Serialise all records first; an invalid record cannot save a subset.
            values = [(name, _json(getattr(self, name))) for name in ("personas", "settings", "targets", "runtime")]
            with self._db:
                self._db.executemany("INSERT INTO documents(name,payload) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload", values)

    def _save_facade(self, name: str) -> None:
        with self._lock:
            self._check_open()
            if name == "settings":
                self._validate_settings(self.settings)
            with self._db:
                self._write_document(name, getattr(self, name))

    def save_personas(self) -> None:
        self._save_facade("personas")

    def save_settings(self) -> None:
        self._save_facade("settings")

    def save_targets(self) -> None:
        self._save_facade("targets")

    def persona(self, persona_id: str | None = None) -> dict[str, Any]:
        wanted = persona_id or self.settings.get("current_persona", "default")
        return next((item for item in self.personas["items"] if item.get("id") == wanted), self.personas["items"][0])

    def upsert_persona(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise StorageError("人设必须是对象")
        with self._lock:
            self._check_open()
            requested_id = _safe_text(value.get("id"), 80)
            old = next((item for item in self.personas["items"] if requested_id and item["id"] == requested_id), {})
            item = _merge(_merge(DEFAULT_PERSONA, old), value)
            item["id"] = requested_id or secrets.token_hex(8)
            item["name"] = _safe_text(item.get("name") or "未命名人设", 100)
            item["updated_at"] = time.time()
            item["created_at"] = old.get("created_at") or item.get("created_at") or item["updated_at"]
            if not isinstance(item.get("state"), dict) or not isinstance(item.get("outfit_pool"), list):
                raise StorageError("人设状态或服装池格式无效")
            item["state"] = {key: _safe_text(item["state"].get(key), 2000) for key in DEFAULT_PERSONA["state"]}
            item["outfit_pool"] = [_safe_text(x, 500) for x in item["outfit_pool"] if _safe_text(x)][:50]
            for key in ("description", "positive_prompt", "negative_prompt", "style_prompt", "consent_prompt"):
                item[key] = _safe_text(item.get(key), 12000)
            if item.get("reference_asset"):
                self.asset(str(item["reference_asset"]))
            references = item.get("reference_assets", [])
            if not isinstance(references, list) or len(references) > 8 or any(not isinstance(name, str) for name in references):
                raise StorageError("参考图列表最多包含 8 张图片")
            for name in references:
                self.asset(name)
            item["reference_assets"] = list(dict.fromkeys(([item["reference_asset"]] if item.get("reference_asset") else []) + references))
            if len(item["reference_assets"]) > 8:
                raise StorageError("参考图库包含主图在内最多 8 张")
            updated = copy.deepcopy(self.personas)
            updated["items"] = [item if existing["id"] == item["id"] else existing for existing in updated["items"]]
            if not old:
                updated["items"].append(item)
            with self._db:
                self._write_document("personas", updated)
            self.personas = updated
            return copy.deepcopy(item)

    def delete_persona(self, persona_id: str) -> bool:
        if persona_id == "default":
            raise StorageError("默认人设不能删除")
        with self._lock:
            self._check_open()
            updated = copy.deepcopy(self.personas)
            updated["items"] = [item for item in updated["items"] if item["id"] != persona_id]
            if len(updated["items"]) == len(self.personas["items"]):
                return False
            settings = copy.deepcopy(self.settings)
            if settings.get("current_persona") == persona_id:
                settings["current_persona"] = "default"
            with self._db:
                self._write_document("personas", updated)
                self._write_document("settings", settings)
                for row in self._db.execute("SELECT umo,payload FROM sessions").fetchall():
                    session = _decoded(row["payload"], "session")
                    if session.get("persona_id") == persona_id:
                        replacement = self._new_session(row["umo"], "default")
                        replacement["revision"] = int(session.get("revision", 0)) + 1
                        self._write_session(replacement)
            self.personas = updated
            # Moderation and other collaborators may retain this facade object.
            self.settings.clear()
            self.settings.update(settings)
            return True

    def _new_session(self, umo: str, persona_id: str) -> dict[str, Any]:
        return {"umo": umo, "persona_id": persona_id, "state": copy.deepcopy(self.persona(persona_id).get("state") or DEFAULT_PERSONA["state"]), "last_image": "", "pending": None, "last_context": [], "revision": 0, "updated_at": time.time()}

    def _write_session(self, value: dict[str, Any]) -> None:
        self._db.execute("INSERT INTO sessions(umo,updated_at,payload) VALUES(?,?,?) ON CONFLICT(umo) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload", (value["umo"], value["updated_at"], _json(value)))

    def read_session(self, umo: str) -> dict[str, Any] | None:
        """Read the current snapshot without creating or switching a persona."""
        umo = _safe_text(umo, 1000)
        if not umo:
            raise StorageError("会话标识不能为空")
        with self._lock:
            self._check_open()
            row = self._db.execute("SELECT payload FROM sessions WHERE umo=?", (umo,)).fetchone()
            return _decoded(row["payload"], "session") if row else None

    def session(self, umo: str, persona_id: str | None = None) -> dict[str, Any]:
        umo = _safe_text(umo, 1000)
        if not umo:
            raise StorageError("会话标识不能为空")
        with self._lock:
            self._check_open()
            row = self._db.execute("SELECT payload FROM sessions WHERE umo=?", (umo,)).fetchone()
            existing = _decoded(row["payload"], "session") if row else None
            wanted = persona_id or (existing or {}).get("persona_id") or self.settings.get("current_persona", "default")
            if wanted not in {item["id"] for item in self.personas["items"]}:
                raise StorageError("找不到该人设")
            if existing and existing.get("persona_id") == wanted:
                return copy.deepcopy(existing)
            value = self._new_session(umo, wanted)
            if existing:
                value["revision"] = int(existing.get("revision", 0)) + 1
            with self._db:
                self._write_session(value)
            return copy.deepcopy(value)

    def save_session(self, umo: str, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise StorageError("会话状态必须是对象")
        umo = _safe_text(umo, 1000)
        if not umo:
            raise StorageError("会话标识不能为空")
        with self._lock:
            self._check_open()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT payload FROM sessions WHERE umo=?", (umo,)).fetchone()
                current = _decoded(row["payload"], "session") if row else self._new_session(umo, str(value.get("persona_id") or self.settings["current_persona"]))
                if "revision" in value and int(value["revision"]) != int(current["revision"]):
                    raise StorageConflictError("会话状态已更新，请重新读取后再试")
                # Pending requests and context are replaced, while a partial
                # state patch preserves the untouched dynamic properties.
                item = {**current, **copy.deepcopy(value), "umo": umo}
                if item.get("persona_id") not in {persona["id"] for persona in self.personas["items"]}:
                    raise StorageError("找不到该人设")
                if not isinstance(item.get("state"), dict):
                    raise StorageError("会话状态格式无效")
                state = _merge(current["state"], item["state"])
                item["state"] = {key: _safe_text(state.get(key), 2000) for key in DEFAULT_PERSONA["state"]}
                if item.get("last_image"):
                    self.asset(str(item["last_image"]))
                item["revision"], item["updated_at"] = int(current["revision"]) + 1, time.time()
                self._write_session(item)
                self._db.commit()
                return copy.deepcopy(item)
            except Exception:
                self._db.rollback()
                raise

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            self._check_open()
            return [_decoded(row["payload"], "session") for row in self._db.execute("SELECT payload FROM sessions ORDER BY updated_at DESC")]

    def claim_photo_request(self, session_key: str, persona_id: str, request_id: str, job_id: str) -> dict[str, Any]:
        """Claim an agreed request once; failures retain the original job claim."""
        return self._claim_photo_request(session_key, persona_id, request_id, job_id)

    def rebind_photo_request(self, session_key: str, persona_id: str, request_id: str, old_job_id: str, new_job_id: str) -> dict[str, Any]:
        """Explicitly retry an existing failure without reopening normal consent."""
        if not old_job_id or old_job_id == new_job_id:
            raise StorageConflictError("重试任务必须对应原来的失败拍摄")
        return self._claim_photo_request(session_key, persona_id, request_id, new_job_id, old_job_id=old_job_id)

    def _claim_photo_request(self, session_key: str, persona_id: str, request_id: str, job_id: str, *, old_job_id: str = "") -> dict[str, Any]:
        if not all(isinstance(item, str) and item for item in (session_key, persona_id, request_id, job_id)):
            raise StorageConflictError("拍摄确认标识不能为空")
        with self._lock:
            self._check_open()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                session = self.read_session(session_key)
                pending = (session or {}).get("pending") or {}
                if not session or session.get("persona_id") != persona_id or not isinstance(pending, dict) or pending.get("session_key") != session_key or pending.get("persona_id") != persona_id or pending.get("request_id") != request_id or pending.get("request_kind") != "photo":
                    raise StorageConflictError("拍摄条件已经更新，请重新确认当前条件")
                if not old_job_id and self._timestamp(pending.get("expires_at")) <= time.time():
                    raise StorageConflictError("拍摄条件已经过期，请重新确认")
                execution_job_id = str(pending.get("execution_job_id") or "")
                if old_job_id:
                    if execution_job_id == job_id:
                        self._db.commit()
                        return {"claimed": False, "job_id": job_id, "session": session}
                    old = self.job(old_job_id)
                    if execution_job_id != old_job_id or not old or old.get("status") not in {"failed", "cancelled"} or old.get("cancel_requested"):
                        raise StorageConflictError("原拍摄不能重试；撤回或条件变化后需要重新判断")
                elif execution_job_id:
                    self._db.commit()
                    return {"claimed": False, "job_id": execution_job_id, "session": session}
                job = self.job(job_id)
                if not job or job.get("status") != "queued" or job.get("session_key") != session_key or job.get("persona_id") != persona_id:
                    raise StorageConflictError("拍摄任务与当前确认条件不一致")
                pending["execution_job_id"] = job_id
                session["pending"] = pending
                session["revision"] = int(session.get("revision", 0)) + 1
                session["updated_at"] = time.time()
                self._write_session(session)
                self._db.commit()
                return {"claimed": True, "job_id": job_id, "session": copy.deepcopy(session)}
            except Exception:
                self._db.rollback()
                raise

    @staticmethod
    def _validate_status(value: dict[str, Any], statuses: frozenset[str]) -> None:
        if value.get("status") not in statuses:
            raise StorageError("任务或发送状态无效")

    def create_job(self, value: dict[str, Any]) -> dict[str, Any]:
        item = {"status": "generating", "at": time.time(), **copy.deepcopy(value)}
        item["id"], item["updated_at"] = _safe_text(item.get("id") or secrets.token_hex(12), 100), time.time()
        self._validate_status(item, JOB_STATUSES)
        item = self._scrub_known_secrets(item)
        with self._lock, self._db:
            self._check_open()
            try:
                self._db.execute("INSERT INTO jobs(id,status,at,updated_at,payload) VALUES(?,?,?,?,?)", (item["id"], item["status"], self._timestamp(item["at"]), item["updated_at"], _json(item)))
            except sqlite3.IntegrityError as exc:
                raise StorageError("任务 ID 已存在") from exc
        return copy.deepcopy(item)

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._check_open()
            row = self._db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            return _decoded(row["payload"], "job") if row else None

    def update_job(self, job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._db:
            item = self.job(job_id)
            if item is None:
                raise StorageError("任务不存在")
            item = {**item, **copy.deepcopy(patch), "id": job_id, "at": item["at"], "updated_at": time.time()}
            self._validate_status(item, JOB_STATUSES)
            item = self._scrub_known_secrets(item)
            self._db.execute("UPDATE jobs SET status=?,updated_at=?,payload=? WHERE id=?", (item["status"], item["updated_at"], _json(item), job_id))
            return copy.deepcopy(item)

    def recent_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            self._check_open()
            return [_decoded(row["payload"], "job") for row in self._db.execute("SELECT payload FROM jobs ORDER BY at DESC,rowid DESC LIMIT ?", (max(0, min(10000, int(limit))),))]

    def reserve_delivery(self, key: str) -> bool:
        key = _safe_text(key, 2000)
        if not key:
            raise StorageError("发送标识不能为空")
        now = time.time()
        item = {"key": key, "status": "claimed", "at": now, "updated_at": now}
        with self._lock, self._db:
            self._check_open()
            cursor = self._db.execute("INSERT OR IGNORE INTO deliveries(key,status,at,updated_at,payload) VALUES(?,?,?,?,?)", (key, "claimed", now, now, _json(item)))
            return cursor.rowcount == 1

    def delivery(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            self._check_open()
            row = self._db.execute("SELECT payload FROM deliveries WHERE key=?", (key,)).fetchone()
            return _decoded(row["payload"], "delivery") if row else None

    def update_delivery(self, key: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._db:
            item = self.delivery(key)
            if item is None:
                raise StorageError("发送记录不存在")
            item = {**item, **copy.deepcopy(patch), "key": key, "at": item["at"], "updated_at": time.time()}
            self._validate_status(item, DELIVERY_STATUSES)
            item = self._scrub_known_secrets(item)
            self._db.execute("UPDATE deliveries SET status=?,updated_at=?,payload=? WHERE key=?", (item["status"], item["updated_at"], _json(item), key))
            return copy.deepcopy(item)

    @staticmethod
    def _quota_day(now: float, tz_name: str) -> str:
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            # Windows may not have IANA tzdata. Defaults need no extra package.
            fallback = {"Asia/Shanghai": timezone(timedelta(hours=8)), "Asia/Hong_Kong": timezone(timedelta(hours=8)), "UTC": timezone.utc, "Etc/UTC": timezone.utc}
            if tz_name not in fallback:
                raise StorageError("该时区在当前 Python 环境不可用") from exc
            tz = fallback[tz_name]
        return datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")

    def record_action(self, value: dict, record_id: str = "") -> dict:
        with self._lock, self._db:
            self._check_open()
            identity = record_id or secrets.token_hex(12)
            row = self._db.execute("SELECT payload FROM actions WHERE id=?", (identity,)).fetchone()
            old = _decoded(row["payload"], "action") if row else {}
            now = time.time()
            item = self._scrub_known_secrets({**old, **copy.deepcopy(value), "id": identity, "at": old.get("at", now), "updated_at": now})
            self._db.execute("INSERT INTO actions(id,session_key,at,updated_at,payload) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET session_key=excluded.session_key,updated_at=excluded.updated_at,payload=excluded.payload", (identity, str(item.get("session_key") or ""), item["at"], now, _json(item)))
            self._db.execute("DELETE FROM actions WHERE id IN (SELECT id FROM actions ORDER BY updated_at DESC LIMIT -1 OFFSET 1000)")
            return copy.deepcopy(item)

    def recent_actions(self, session_key: str = "", limit: int = 50) -> list[dict]:
        with self._lock:
            self._check_open()
            condition, args = ("WHERE session_key=?", [session_key]) if session_key else ("", [])
            rows = self._db.execute(f"SELECT payload FROM actions {condition} ORDER BY updated_at DESC LIMIT ?", (*args, max(1, min(200, int(limit)))))
            return [_decoded(row["payload"], "action") for row in rows]

    def reserve_budget(self, kind: str, limits: dict) -> str | None:
        if kind not in {"messages", "photos", "llm"}:
            raise StorageError("主动预算类别无效")
        now = time.time()
        day = self._quota_day(now, str(limits.get("timezone") or "Asia/Shanghai"))
        maximum = max(0, int(limits.get(kind, 0)))
        with self._lock:
            self._check_open()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute("DELETE FROM budget_reservations WHERE expires_at<?", (now,))
                row = self._db.execute("SELECT used FROM budget_usage WHERE kind=? AND day=?", (kind, day)).fetchone()
                used = int(row[0]) if row else 0
                reserved = self._db.execute("SELECT COUNT(*) FROM budget_reservations WHERE kind=? AND day=?", (kind, day)).fetchone()[0]
                token = secrets.token_hex(12) if used + reserved < maximum else None
                if token:
                    self._db.execute("INSERT INTO budget_reservations VALUES(?,?,?,?)", (token, kind, day, now + 900))
                self._db.commit()
                return token
            except Exception:
                self._db.rollback()
                raise

    def finish_budget(self, token: str | None, used: bool = False):
        if not token:
            return
        with self._lock, self._db:
            self._check_open()
            row = self._db.execute("SELECT kind,day FROM budget_reservations WHERE id=?", (token,)).fetchone()
            if row:
                if used:
                    self._db.execute("INSERT INTO budget_usage(kind,day,used) VALUES(?,?,1) ON CONFLICT(kind,day) DO UPDATE SET used=used+1", (row["kind"], row["day"]))
                self._db.execute("DELETE FROM budget_reservations WHERE id=?", (token,))

    def budget_summary(self, limits: dict) -> dict:
        now = time.time()
        day = self._quota_day(now, str(limits.get("timezone") or "Asia/Shanghai"))
        result = {"day": day, "timezone": limits.get("timezone", "Asia/Shanghai")}
        with self._lock:
            self._check_open()
            for kind in ("messages", "photos", "llm"):
                row = self._db.execute("SELECT used FROM budget_usage WHERE kind=? AND day=?", (kind, day)).fetchone()
                used = int(row[0]) if row else 0
                reserved = self._db.execute("SELECT COUNT(*) FROM budget_reservations WHERE kind=? AND day=? AND expires_at>?", (kind, day, now)).fetchone()[0]
                limit = max(0, int(limits.get(kind, 0)))
                result[kind] = {"used": used, "reserved": reserved, "limit": limit, "remaining": max(0, limit - used - reserved)}
        return result

    def reserve_quota(self, user_key: str, limits: dict[str, Any], is_admin: bool = False) -> tuple[bool, str]:
        user_key = _safe_text(user_key, 1000)
        if not user_key:
            raise StorageError("用户标识不能为空")
        now = time.time()
        day = self._quota_day(now, str(limits.get("timezone") or "Asia/Shanghai"))
        limit, interval = max(0, int(limits.get("daily_limit", 5))), max(0, int(limits.get("min_interval_sec", 20)))
        concurrency, enforced = max(1, int(limits.get("max_concurrency", 1))), limits.get("enabled") is not False
        lease = max(60, int(limits.get("reservation_timeout_sec", max(720, int(self.settings.get("generation", {}).get("timeout_sec", 180)) + 120))))
        with self._lock:
            self._check_open()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute("DELETE FROM quota_reservations WHERE expires_at<?", (now,))
                row = self._db.execute("SELECT successes FROM quota_usage WHERE user_key=? AND day=?", (user_key, day)).fetchone()
                successes = int(row["successes"]) if row else 0
                reservations = self._db.execute("SELECT day FROM quota_reservations WHERE user_key=?", (user_key,)).fetchall()
                last = self._db.execute("SELECT MAX(last_attempt) FROM quota_usage WHERE user_key=?", (user_key,)).fetchone()[0] or 0
                reason = ""
                if enforced and not is_admin and len(reservations) >= concurrency:
                    reason = "上一张图片还在生成，请稍候。"
                elif enforced and not is_admin:
                    if limit and successes + sum(row["day"] == day for row in reservations) >= limit:
                        reason = "今天的生图次数已用完，请明天再试。"
                    elif last and interval and now - last < interval:
                        reason = f"请求太频繁，请 {int(interval - (now - last)) + 1} 秒后再试。"
                if reason:
                    self._db.commit()
                    return False, reason
                self._db.execute("INSERT INTO quota_usage(user_key,day,successes,last_attempt) VALUES(?,?,0,?) ON CONFLICT(user_key,day) DO UPDATE SET last_attempt=excluded.last_attempt", (user_key, day, now))
                self._db.execute("INSERT INTO quota_reservations(user_key,day,at,expires_at,is_admin) VALUES(?,?,?,?,?)", (user_key, day, now, now + lease, int(is_admin)))
                self._db.commit()
                return True, ""
            except Exception:
                self._db.rollback()
                raise

    def finish_quota(self, user_key: str, success: bool = False) -> None:
        with self._lock:
            self._check_open()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT * FROM quota_reservations WHERE user_key=? ORDER BY id LIMIT 1", (user_key,)).fetchone()
                if row:
                    self._db.execute("DELETE FROM quota_reservations WHERE id=?", (row["id"],))
                    if success and not row["is_admin"]:
                        self._db.execute("UPDATE quota_usage SET successes=successes+1 WHERE user_key=? AND day=?", (user_key, row["day"]))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def _scrub_known_secrets(self, value: Any) -> Any:
        known: set[str] = set()
        def collect(item: Any) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    if _secret_key(key) and isinstance(nested, str) and len(nested) >= 6:
                        known.add(nested)
                    else:
                        collect(nested)
            elif isinstance(item, list):
                for nested in item:
                    collect(nested)
        collect(getattr(self, "settings", {}))
        def scrub(item: Any) -> Any:
            if isinstance(item, dict):
                return {key: scrub(nested) for key, nested in item.items() if not _secret_key(key)}
            if isinstance(item, list):
                return [scrub(nested) for nested in item]
            if isinstance(item, str):
                for secret in sorted(known, key=len, reverse=True):
                    item = item.replace(secret, "[REDACTED]")
            return item
        return scrub(copy.deepcopy(value))

    def safe_settings(self) -> dict[str, Any]:
        with self._lock:
            return self._scrub_known_secrets(_redacted(self.settings))

    def append_history(self, item: dict[str, Any]) -> None:
        value = self._scrub_known_secrets({"at": time.time(), **item})
        with self._lock, self._db:
            self._check_open()
            self._db.execute("INSERT INTO history(at,payload) VALUES(?,?)", (self._timestamp(value["at"]), _json(value)))

    def recent_history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            self._check_open()
            rows = self._db.execute("SELECT payload FROM history ORDER BY id DESC LIMIT ?", (max(0, min(10000, int(limit))),)).fetchall()
            return [_decoded(row["payload"], "history") for row in reversed(rows)]

    def cleanup_history(self, limit: int) -> dict[str, int]:
        """Bound history/finished jobs and retain delivery records for 30 days.

        In-flight jobs and deliveries always survive. A newly generated chat
        image gets a five-minute grace period to enter its sending stage.
        Images referenced by any surviving record remain on disk.
        """
        limit = max(0, int(limit))
        with self._lock, self._db:
            self._check_open()
            self._db.execute("BEGIN IMMEDIATE")
            rows = self._db.execute("SELECT id,payload FROM history ORDER BY id DESC LIMIT -1 OFFSET ?", (limit,)).fetchall()
            deleted = [_decoded(row["payload"], "history") for row in rows]
            now = time.time()
            delivery_rows = self._db.execute("SELECT key,payload FROM deliveries WHERE updated_at<? AND status IN ('sent','skipped','failed','uncertain')", (now - 30 * 86400,)).fetchall()
            expired_delivery_keys = {row["key"] for row in delivery_rows}
            protected_jobs = set()
            for row in self._db.execute("SELECT key,payload FROM deliveries"):
                if row["key"] not in expired_delivery_keys:
                    value = _decoded(row["payload"], "delivery")
                    if value.get("job_id"):
                        protected_jobs.add(str(value["job_id"]))
            for row in self._db.execute("SELECT payload FROM sessions"):
                pending = _decoded(row["payload"], "session").get("pending") or {}
                if isinstance(pending, dict) and pending.get("execution_job_id") and self._timestamp(pending.get("expires_at")) > now:
                    protected_jobs.add(str(pending["execution_job_id"]))
            finished = self._db.execute("SELECT id,status,updated_at,payload FROM jobs WHERE status IN ('succeeded','failed','sent','uncertain','cancelled') ORDER BY updated_at DESC,rowid DESC").fetchall()
            eligible_finished = []
            for row in finished:
                value = _decoded(row["payload"], "job")
                if row["id"] in protected_jobs:
                    continue
                if row["status"] == "succeeded" and value.get("source") != "webui" and row["updated_at"] > now - 300:
                    continue
                eligible_finished.append(row)
            pruned_jobs = eligible_finished[limit:]
            deleted.extend(_decoded(row["payload"], "job") for row in pruned_jobs)
            deleted.extend(_decoded(row["payload"], "delivery") for row in delivery_rows)
            self._db.executemany("DELETE FROM history WHERE id=?", [(row["id"],) for row in rows])
            self._db.executemany("DELETE FROM jobs WHERE id=?", [(row["id"],) for row in pruned_jobs])
            self._db.executemany("DELETE FROM deliveries WHERE key=?", [(row["key"],) for row in delivery_rows])
            keep, candidates = self._asset_references(now), set()
            for item in deleted:
                self._gather_asset_names(item, candidates)
            removed = 0
            for name in candidates - keep:
                try:
                    self.asset(name).unlink()
                    removed += 1
                except (StorageError, OSError):
                    continue
            return {"history_deleted": len(rows), "jobs_deleted": len(pruned_jobs), "deliveries_deleted": len(delivery_rows), "assets_deleted": removed}

    @staticmethod
    def _gather_asset_names(value: Any, target: set[str]) -> None:
        if isinstance(value, dict):
            for item in value.values():
                Storage._gather_asset_names(item, target)
        elif isinstance(value, list):
            for item in value:
                Storage._gather_asset_names(item, target)
        elif isinstance(value, str):
            name = value.replace("\\", "/").rsplit("/", 1)[-1]
            if name and "." in name:
                target.add(name)

    def _asset_references(self, now: float) -> set[str]:
        keep: set[str] = set()
        # Read committed documents, including updates from another connection.
        for table in ("documents", "history", "sessions", "jobs", "deliveries", "actions"):
            for row in self._db.execute(f"SELECT payload FROM {table}"):
                self._gather_asset_names(_decoded(row["payload"], table), keep)
        keep.update(row["name"] for row in self._db.execute("SELECT name FROM asset_leases WHERE expires_at>?", (now,)))
        return keep

    def lease_asset(self, name: str, ttl: int = 3600) -> str:
        """Protect a prepared reference until its owning job records the asset."""
        ttl = max(1, min(86400, int(ttl)))
        with self._lock, self._db:
            self._check_open()
            self._db.execute("BEGIN IMMEDIATE")
            self.asset(name)
            token = secrets.token_hex(12)
            self._db.execute("INSERT INTO asset_leases(id,name,expires_at) VALUES(?,?,?)", (token, name, time.time() + ttl))
            return token

    def release_asset(self, token: str | None) -> None:
        if not token:
            return
        with self._lock, self._db:
            self._check_open()
            self._db.execute("DELETE FROM asset_leases WHERE id=?", (str(token),))

    def cleanup_assets(self, grace_sec: int = 3600) -> dict[str, int]:
        """Remove only unreferenced assets older than the upload grace period."""
        grace_sec = max(0, int(grace_sec))
        with self._lock, self._db:
            self._check_open()
            self._db.execute("BEGIN IMMEDIATE")
            now = time.time()
            expired = self._db.execute("DELETE FROM asset_leases WHERE expires_at<=?", (now,)).rowcount
            keep = self._asset_references(now)
            removed = 0
            for path in self.assets.iterdir():
                if path.name in keep or path.name.startswith(".") or path.is_symlink():
                    continue
                try:
                    if not path.is_file() or path.stat().st_mtime > now - grace_sec:
                        continue
                    self.asset(path.name).unlink()
                    removed += 1
                except (StorageError, OSError):
                    continue
            return {"assets_deleted": removed, "leases_expired": expired}

    def asset(self, name: str) -> Path:
        if not isinstance(name, str) or not name or len(name) > 255 or name.startswith(".") or any(char in name for char in ("/", "\\", ":", "\x00")) or Path(name).name != name:
            raise StorageError("图片资源名称无效")
        candidate = self.assets / name
        if candidate.suffix.lower().lstrip(".") not in _ASSET_SUFFIXES:
            raise StorageError("图片资源扩展名无效")
        try:
            candidate.resolve().relative_to(self.assets.resolve())
        except ValueError as exc:
            raise StorageError("图片资源路径越界") from exc
        if not candidate.is_file():
            raise FileNotFoundError("图片资源不存在")
        return candidate

    def save_asset(self, data: bytes, suffix: str = "png") -> Path:
        suffix = str(suffix).lower().lstrip(".")
        if suffix not in _ASSET_SUFFIXES or not re.fullmatch(r"[a-z]{2,5}", suffix):
            raise StorageError("图片资源扩展名无效")
        if not isinstance(data, bytes) or not data or len(data) > 64 * 1024 * 1024:
            raise StorageError("图片内容为空或超过 64 MB")
        path = self.assets / f"{hashlib.sha256(data).hexdigest()[:24]}.{suffix}"
        with self._lock:
            self._check_open()
            if path.exists():
                existing = self.asset(path.name)
                # Reusing an old image is still a fresh upload draft.
                os.utime(existing, None)
                return existing
            fd, temporary = tempfile.mkstemp(prefix=".asset-", dir=str(self.assets))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return path

    def webui_token(self, configured: str = "") -> str:
        if configured.strip():
            return configured.strip()
        path = self.root / "webui_token.txt"
        with self._lock:
            if path.exists():
                token = path.read_text(encoding="utf-8").strip()
                if token:
                    return token
            token = secrets.token_urlsafe(32)
            path.write_text(token + "\n", encoding="utf-8")
            try:
                path.chmod(0o600)
            except OSError:
                pass
            return token

    def export(self, include_secrets: bool = False) -> dict[str, Any]:
        """Portable data export; credentials are excluded by default."""
        with self._lock:
            self._check_open()
            value = {"version": SCHEMA_VERSION, "exported_at": time.time(), "personas": copy.deepcopy(self.personas), "settings": copy.deepcopy(self.settings), "targets": copy.deepcopy(self.targets), "runtime": copy.deepcopy(self.runtime), "sessions": self.list_sessions(), "jobs": [_decoded(row["payload"], "job") for row in self._db.execute("SELECT payload FROM jobs ORDER BY at DESC")], "history": [_decoded(row["payload"], "history") for row in self._db.execute("SELECT payload FROM history ORDER BY id")], "deliveries": [_decoded(row["payload"], "delivery") for row in self._db.execute("SELECT payload FROM deliveries ORDER BY at")], "assets": [path.name for path in self.assets.iterdir() if path.is_file() and not path.name.startswith(".")]}
            value["actions"] = self.recent_actions(limit=200)
            value["proactive_budget_usage"] = [dict(row) for row in self._db.execute("SELECT kind,day,used FROM budget_usage ORDER BY day DESC")]
            return value if include_secrets else self._scrub_known_secrets(_redacted(value))

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
