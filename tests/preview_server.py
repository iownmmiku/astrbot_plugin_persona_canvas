"""Local offline WebUI QA harness. Never uses the real AstrBot data directory.

Run: py tests/preview_server.py
Open http://127.0.0.1:39718 and enter preview-test-token.
The real plugin and WebUI transport run against test_core fixtures. Prompts
containing 拒绝/条件 exercise refusal/confirmation; others generate a clearly
labelled placeholder. --decision provides a fixed LLM decision when needed.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import signal
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

# Loading test_core installs its AstrBot module stubs before importing main.
from test_core import Context, Event, ImageProvider, LLM, Storage, main
from canvas_test.providers.base import GeneratedImage, ProviderCapabilities
from PIL import Image, ImageDraw, ImageFont

HOST, PORT, TOKEN = "127.0.0.1", 39718, "preview-test-token"
PERSONA_ID = "preview-character"


def fixture_image(width: int = 640, height: int = 832) -> bytes:
    """A real PNG whose visible label prevents confusion with model output."""
    width, height = max(256, min(1024, int(width))), max(256, min(1536, int(height)))
    image = Image.new("RGB", (width, height), "#edf5f2")
    draw = ImageDraw.Draw(image)
    margin = max(16, width // 18)
    draw.rounded_rectangle((margin, margin, width - margin, height - margin), radius=24, fill="#ffffff", outline="#aaccc3", width=3)
    center_x, center_y = width // 2, height // 2
    radius = min(width, height) // 8
    draw.ellipse((center_x - radius, center_y - radius - 65, center_x + radius, center_y + radius - 65), fill="#c7e3db")
    draw.rounded_rectangle((center_x - radius - 35, center_y + radius - 55, center_x + radius + 35, center_y + radius + 55), radius=25, fill="#dcece7")
    font = ImageFont.load_default()
    draw.text((center_x, margin + 40), "OFFLINE UI PREVIEW", font=font, fill="#284d46", anchor="mm")
    draw.text((center_x, height - margin - 75), "Fake generation - no API request", font=font, fill="#284d46", anchor="mm")
    draw.text((center_x, height - margin - 45), f"{width} x {height} / temporary data", font=font, fill="#537870", anchor="mm")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class PreviewLLM(LLM):
    def __init__(self, decision: str = "auto"):
        super().__init__()
        self.decision = decision
        self.next = None  # Tests may set a complete decision dict explicitly.

    def meta(self):
        return types.SimpleNamespace(id="chat", model="offline-preview-chat", type="openai")

    async def get_models(self):
        return ["offline-preview-chat"]

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        prompt = str(kwargs.get("prompt") or "")
        if "PONG" in prompt.upper():
            return types.SimpleNamespace(completion_text="PONG")
        if isinstance(self.next, dict):
            value = dict(self.next)
        else:
            action = self.decision
            if action == "auto":
                if "拒绝" in prompt:
                    action = "refuse"
                elif "条件" in prompt:
                    action = "ask"
                elif "只允许文字" in prompt or "只发文字" in prompt:
                    action = "chat"
                elif "编辑参考" in prompt or "改图" in prompt or "改成夜景" in prompt:
                    action = "edit"
                elif "普通场景" in prompt or "画一片" in prompt or "画一只" in prompt:
                    action = "scene"
                else:
                    action = "photo"
            replies = {
                "refuse": "这是离线拒绝测试：今天我不想拍，先陪你聊聊吧。",
                "ask": "这是离线条件测试：拍远一点、穿白裙，可以吗？",
                "photo": "这是离线测试照片，图片由本地占位图生成。",
                "scene": "这是离线场景绘图测试，图片由本地占位图生成。",
                "edit": "这是离线参考图编辑测试，图片由本地占位图生成。",
                "state": "这是离线换装测试，我已经换好白色连衣裙啦。",
                "chat": "这是离线文字测试，今天想聊些什么？",
                "skip": "",
            }
            value = {"decision": action, "reply": replies[action], "prompt": "", "state_patch": {}, "needs_reference": action == "edit"}
            if action in {"photo", "scene", "edit"}:
                value["prompt"] = "offline preview illustration, soft daylight, clean composition; " + prompt[:800]
            if action == "state" or (action == "photo" and "白裙" in prompt):
                value["state_patch"] = {"outfit": "white dress", "expression": "gentle smile"}
        return types.SimpleNamespace(completion_text=json.dumps(value, ensure_ascii=False))


class PreviewProvider(ImageProvider):
    def __init__(self, name: str, config: dict, delay: float = 1.2):
        super().__init__()
        self.name, self.config, self.delay = name, config, delay
        self.capabilities = ProviderCapabilities(
            image_to_image=bool(config.get("supports_image_edit", True)),
            negative_prompt=True, seed=True, sampler=True,
            dimensions="离线测试支持自定义尺寸",
            identity_reference="离线占位图，仅验证界面和参考图传递",
            negative_prompt_mode="离线测试参数记录", automated_generation=True, max_reference_images=3,
        )

    async def generate(self, positive, negative, *, reference=None, options=None):
        options = options or {}
        self.calls.append({"positive": positive, "negative": negative, "reference": reference, "options": dict(options)})
        await asyncio.sleep(max(0, self.delay))
        if self.error:
            raise ValueError(self.error)
        image = fixture_image(int(options.get("width", 640)), int(options.get("height", 832)))
        return GeneratedImage(image, "png", self.name, str(self.config.get("model") or "offline-preview-image"))

    async def list_models(self):
        return {"models": ["offline-preview-image", "offline-preview-square"], "automatic": True, "note": "离线测试模型；没有发送网络请求。"}

    async def test_connection(self):
        return {"ok": True, "provider": self.name, "model": self.config.get("model", ""), "status_code": 200, "elapsed_ms": 0, "message": "离线接口连接测试成功，没有发送网络请求。"}

    async def test_generation(self, prompt="simple blue flower on white background"):
        started = time.monotonic()
        result = await self.generate(prompt, "", options={"width": 512, "height": 640})
        return result, int((time.monotonic() - started) * 1000)


async def create_preview(directory: Path, *, port: int = PORT, decision: str = "auto", delay: float = 1.2):
    """Construct fixtures without starting a server; safe for import-time QA."""
    context = Context()
    context.llm = PreviewLLM(decision)
    context.conversation_manager.cid = ""
    store = Storage(directory)
    config = {"enable_webui": True, "enable_legacy_webui": True, "webui_host": HOST, "webui_port": port, "webui_token": TOKEN}
    try:
        with patch.object(main, "Storage", return_value=store):
            plugin = main.PersonaCanvasPlugin(context, config)
        reference = store.save_asset(fixture_image())
        persona = store.upsert_persona({
            "id": PERSONA_ID, "name": "测试角色（离线 UI 验证）", "astrbot_persona_id": "shy",
            "description": "仅供本地界面验证的虚构测试角色，不连接真实模型。",
            "consent_prompt": "测试输入包含“拒绝”时拒绝、包含“条件”时询问，其余明确请求可以生成离线占位图。",
            "positive_prompt": "adult fictional character, silver hair, green eyes",
            "style_prompt": "soft anime illustration, clean lines",
            "state": {"outfit": "white dress", "expression": "gentle smile", "pose": "standing", "scene": "bright room"},
            "outfit_pool": ["white dress", "casual hoodie"],
            "reference_enabled": True, "reference_asset": reference.name,
        })
        store.settings["current_persona"] = persona["id"]
        store.settings["providers"] = {"default": {
            "name": "default", "kind": "openai", "endpoint": "https://offline.invalid/v1",
            "model": "offline-preview-image", "api_key": "offline-fixture-key",
            "supports_image_edit": True, "supports_seed": True, "negative_prompt": True,
            "auth_header": "Authorization", "auth_prefix": "Bearer ", "timeout": 30,
        }}
        store.settings["default_provider"] = "default"
        store.settings["moderation"]["min_interval_sec"] = 0
        store.settings["active"]["enabled"] = False
        store.settings["good_morning"]["enabled"] = False
        store.settings["llm"]["provider_id"] = "chat"
        providers = {}
        def provider(name=None):
            selected = name or store.settings["default_provider"]
            provider_config = store.settings["providers"].get(selected)
            if provider_config is None:
                raise ValueError("测试接口不存在")
            instance = PreviewProvider(selected, dict(provider_config), delay)
            providers[selected] = instance
            return instance
        plugin._provider = provider
        plugin.preview_providers = providers
        plugin.preview_context = context
        # Create the private target through the actual scheduler observation path.
        observer = main.ActiveScheduler(plugin)
        observer.observe(Event("这是离线 UI 测试会话", "qq:FriendMessage:preview"))
        target = store.targets["items"][0]
        target.update(persona_id=PERSONA_ID, enabled=False, morning_enabled=False)
        session = store.session("webui:studio", PERSONA_ID)
        session["last_image"] = reference.name
        store.save_session("webui:studio", session)
        initial = store.create_job({"status": "succeeded", "source": "webui", "mode": "persona", "persona_id": PERSONA_ID, "asset": reference.name, "provider": "default", "model": "offline-preview-image", "caption": "离线占位图，供界面布局检查", "prompt": "offline preview fixture", "state_after": persona["state"]})
        store.append_history({"ok": True, "job_id": initial["id"], "image": "/assets/" + reference.name, "caption": initial["caption"], "provider": "default", "model": "offline-preview-image", "mode": "persona"})
        store.save_all()
        return plugin, context
    except BaseException:
        store.close()
        raise


async def serve(args):
    with tempfile.TemporaryDirectory(prefix="persona-canvas-preview-") as directory:
        plugin = None
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        def request_stop(*_args):
            loop.call_soon_threadsafe(stop.set)
        previous = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        try:
            plugin, _context = await create_preview(Path(directory), port=args.port, decision=args.decision, delay=args.delay)
            await plugin.initialize()
            print(f"Offline preview: {plugin.webui.url}", flush=True)
            print(f"Test token: {TOKEN}", flush=True)
            print("All model/platform calls are fake; data lives in a temporary directory.", flush=True)
            print("Inputs: 拒绝 -> refusal; 条件 -> confirmation; ordinary requests -> placeholder.", flush=True)
            print("Press Ctrl+C to stop and remove temporary data.", flush=True)
            await stop.wait()
        finally:
            if plugin is not None:
                await plugin.terminate()
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--decision", choices=("auto", "photo", "scene", "edit", "ask", "refuse", "state", "chat", "skip"), default="auto")
    parser.add_argument("--delay", type=float, default=1.2, help="Fake generation delay in seconds.")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(serve(parse_args()))
    except KeyboardInterrupt:
        pass
