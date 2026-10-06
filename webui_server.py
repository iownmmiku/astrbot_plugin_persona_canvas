from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class WebUI:
    ASSETS = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/app.css": ("app.css", "text/css; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    }

    def __init__(self, plugin, *, host: str, port: int, token: str):
        if not token or len(token.encode()) > 512:
            raise ValueError("WebUI 令牌不能为空且不能超过 512 字节")
        self.plugin = plugin
        self.host, self.port, self.token = host, port, token
        self.loop = asyncio.get_running_loop()
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.closing = False

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        return f"http://{host}:{self.port}"

    def start(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "PersonaCanvas"
            sys_version = ""

            def log_message(self, *_args):
                pass

            def reply(self, status: int, body: object, mime: str = "application/json; charset=utf-8"):
                if not isinstance(body, bytes):
                    body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def authorized(self) -> bool:
                value = self.headers.get("Authorization", "")
                return hmac.compare_digest(value, f"Bearer {owner.token}")

            def same_origin(self) -> bool:
                origin = self.headers.get("Origin")
                if origin:
                    parsed = urlsplit(origin)
                    if parsed.netloc != self.headers.get("Host") or parsed.scheme not in {"http", "https"}:
                        return False
                return self.headers.get("Sec-Fetch-Site") not in {"cross-site", "same-site"}

            def dispatch(self, coroutine):
                if owner.closing:
                    coroutine.close()
                    raise ApiError(503, "控制台正在关闭")
                future = asyncio.run_coroutine_threadsafe(coroutine, owner.loop)
                try:
                    return future.result(timeout=30)
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    raise ApiError(504, "请求超时") from None

            def do_GET(self):
                try:
                    path = urlsplit(self.path).path
                    if path in owner.ASSETS:
                        name, mime = owner.ASSETS[path]
                        self.reply(200, (Path(__file__).parent / "webui" / name).read_bytes(), mime)
                        return
                    if path.startswith("/assets/"):
                        name = unquote(path.removeprefix("/assets/"))
                        if "/" in name or "\\" in name or not name or name.startswith("."):
                            raise ApiError(404, "资源不存在")
                        asset = owner.plugin.storage.assets / name
                        if not asset.is_file() or asset.stat().st_size > 64 * 1024 * 1024:
                            raise ApiError(404, "资源不存在")
                        mime = "image/png" if asset.suffix.lower() == ".png" else "image/jpeg" if asset.suffix.lower() in {".jpg", ".jpeg"} else "image/webp"
                        self.reply(200, asset.read_bytes(), mime)
                        return
                    if not self.authorized():
                        raise ApiError(401, "请输入有效的访问令牌")
                    routes = {
                        "/api/state": owner.plugin.web_state,
                        "/api/personas": owner.plugin.web_personas,
                        "/api/providers": owner.plugin.web_providers,
                        "/api/targets": owner.plugin.web_targets,
                        "/api/history": owner.plugin.web_history,
                    }
                    callback = routes.get(path)
                    if callback is None:
                        raise ApiError(404, "页面不存在")
                    self.reply(200, self.dispatch(callback()))
                except ApiError as exc:
                    self.reply(exc.status, {"error": str(exc)})
                except Exception:
                    self.reply(500, {"error": "读取失败，请查看 AstrBot 日志"})

            def do_POST(self):
                try:
                    if not self.authorized():
                        raise ApiError(401, "请输入有效的访问令牌")
                    if not self.same_origin():
                        raise ApiError(403, "只允许从控制台页面执行操作")
                    path = urlsplit(self.path).path
                    try:
                        size = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        raise ApiError(400, "请求长度无效") from None
                    if not 0 < size <= 2 * 1024 * 1024:
                        raise ApiError(413, "请求内容为空或过大")
                    try:
                        body = json.loads(self.rfile.read(size))
                    except (ValueError, UnicodeError):
                        raise ApiError(400, "JSON 格式无效") from None
                    if not isinstance(body, dict):
                        raise ApiError(400, "请求必须是对象")
                    routes = {
                        "/api/personas": owner.plugin.web_save_persona,
                        "/api/providers": owner.plugin.web_save_provider,
                        "/api/settings": owner.plugin.web_save_settings,
                        "/api/generate": owner.plugin.web_generate,
                        "/api/targets": owner.plugin.web_target_action,
                    }
                    callback = routes.get(path)
                    if callback is None:
                        raise ApiError(404, "接口不存在")
                    self.reply(200, self.dispatch(callback(body)))
                except ApiError as exc:
                    self.reply(exc.status, {"error": str(exc)})
                except Exception:
                    self.reply(500, {"error": "操作失败，请查看 AstrBot 日志"})

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server((self.host, self.port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, name="persona-canvas-webui", daemon=True)
        self.thread.start()

    async def close(self):
        self.closing = True
        if self.server:
            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()
        if self.thread:
            await asyncio.to_thread(self.thread.join, 2)
