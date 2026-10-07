from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hmac
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .page_api import MAX_UPLOAD_BYTES, asset_path, dispatch_api, public_error


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class WebUI:
    """Opt-in legacy transport; serves exactly the same UI as Plugin Pages."""
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
        self.server = None
        self.thread = None
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

            def reply(self, status: int, body, mime: str = "application/json; charset=utf-8"):
                if not isinstance(body, bytes):
                    body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def authorized(self) -> bool:
                return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {owner.token}")

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
                    return future.result(timeout=180)
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    raise ApiError(504, "请求超时") from None

            def do_GET(self):
                try:
                    path = urlsplit(self.path).path
                    if path in owner.ASSETS:
                        name, mime = owner.ASSETS[path]
                        self.reply(200, (Path(__file__).parent / "pages" / "canvas" / name).read_bytes(), mime)
                        return
                    if not self.authorized():
                        raise ApiError(401, "请输入有效的访问令牌")
                    if path.startswith("/assets/") or path.startswith("/api/page/assets/"):
                        name = unquote(path.rsplit("/", 1)[-1])
                        asset = asset_path(owner.plugin, name)
                        self.reply(200, asset.read_bytes(), mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
                        return
                    if not path.startswith("/api/"):
                        raise ApiError(404, "页面不存在")
                    self.reply(200, self.dispatch(dispatch_api(owner.plugin, "GET", path.removeprefix("/api/"))))
                except ApiError as exc:
                    self.reply(exc.status, {"status": "error", "message": str(exc)})
                except LookupError as exc:
                    self.reply(404, {"status": "error", "message": str(exc)})
                except Exception as exc:
                    self.reply(400, {"status": "error", "message": public_error(owner.plugin, exc)})

            def do_POST(self):
                try:
                    if not self.authorized():
                        raise ApiError(401, "请输入有效的访问令牌")
                    if not self.same_origin():
                        raise ApiError(403, "只允许从控制台页面执行操作")
                    path = urlsplit(self.path).path
                    if not path.startswith("/api/"):
                        raise ApiError(404, "接口不存在")
                    try:
                        size = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        raise ApiError(400, "请求长度无效") from None
                    if not 0 < size <= 24 * 1024 * 1024:
                        raise ApiError(413, "请求内容为空或过大")
                    try:
                        body = json.loads(self.rfile.read(size))
                    except (ValueError, UnicodeError):
                        raise ApiError(400, "JSON 格式无效") from None
                    if not isinstance(body, dict):
                        raise ApiError(400, "请求必须是对象")
                    suffix = path.removeprefix("/api/").removeprefix("page/")
                    if suffix == "reference/upload":
                        data = base64.b64decode(str(body.get("data") or ""), validate=True)
                        if len(data) > MAX_UPLOAD_BYTES:
                            raise ApiError(413, "参考图不能超过 16 MB")
                        result = self.dispatch(owner.plugin.web_upload_reference(data, str(body.get("name") or "reference.png"), str(body.get("persona_id") or "")))
                    else:
                        result = self.dispatch(dispatch_api(owner.plugin, "POST", suffix, body))
                    self.reply(200, result)
                except ApiError as exc:
                    self.reply(exc.status, {"status": "error", "message": str(exc)})
                except LookupError as exc:
                    self.reply(404, {"status": "error", "message": str(exc)})
                except Exception as exc:
                    self.reply(400, {"status": "error", "message": public_error(owner.plugin, exc)})

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
