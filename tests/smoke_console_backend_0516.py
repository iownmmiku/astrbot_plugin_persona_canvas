"""Chrome -> real plugin routes -> temporary SQLite; model/platform IO is fake."""
import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from preview_server import create_preview, PreviewProvider, TOKEN
from test_core import Event, Storage, main
from canvas_test.page_api import POST_ROUTES


async def run(node):
    with tempfile.TemporaryDirectory(prefix="canvas-backend-smoke-") as directory:
        root = Path(directory)
        plugin, context = await create_preview(root, port=0, delay=.05)
        latest = [plugin]
        counters = {"generations": 0}
        restart = None

        def install(current):
            def provider(name=None):
                selected = name or current.storage.settings["default_provider"]
                instance = PreviewProvider(selected, current.storage.settings["providers"][selected], .05)
                generate = instance.generate
                async def count(*args, **kwargs):
                    counters["generations"] += 1
                    return await generate(*args, **kwargs)
                instance.generate = count
                return instance
            current._provider = provider
            current.qa_control = control

        async def reopen():
            await asyncio.sleep(.15)
            old = latest[0]
            port = old.webui.port
            config = dict(old.config)
            config["webui_port"] = port
            await old.terminate()
            store = Storage(root)
            with patch.object(main, "Storage", return_value=store):
                current = main.PersonaCanvasPlugin(context, config)
            install(current)
            latest[0] = current
            await current.initialize()

        async def control(body):
            nonlocal restart
            current = latest[0]
            action = body.get("action")
            if action == "restart":
                restart = asyncio.create_task(reopen())
                return {"ok": True}
            if action == "snapshot":
                config = current.storage.settings["providers"]["default"]
                return {"secret_preserved": config.get("extra_body", {}).get("request", {}).get("token") == "fixture-cross-layer-secret", "generations": counters["generations"], "sent": len(context.sent), "max_wait_sec": current.storage.settings["reply_images"]["max_wait_sec"]}
            if action == "seed_uncertain":
                async for _ in current.cancel_photo(Event("取消拍摄", "qq:FriendMessage:preview")):
                    pass
                env = await current.dialogue.environment("qq:FriendMessage:preview")
                job = await current._generate_job(env, "offline portrait", raw="拍一张自拍", is_admin=True)
                job = current.storage.update_job(job["id"], {"status": "uncertain", "source": "chat", "delivery_evidence": "unverified"})
                return {"job_id": job["id"]}
            if action == "replace_conditions":
                current._provider = lambda name=None: SlowProvider()
                event = Event("给我拍一张白裙自拍", "qq:FriendMessage:preview")
                env = await current._env(event)
                pending = current._save_conditions(env, event.message_str, "白裙远景，可以吗？", {"outfit": "white dress", "camera": "wide shot"})
                queued = json.loads(await current.tool_photo(Event("就按你说的拍", event.unified_msg_origin), "white dress portrait", request_summary="确认原条件", confirmed_request_id=pending["request_id"]))
                if not queued.get("ok"):
                    raise AssertionError(queued)
                for _ in range(100):
                    if current.storage.job(queued["job_id"])["status"] == "generating":
                        break
                    await asyncio.sleep(.01)
                result = json.loads(await current.tool_conditions(Event("改成外套再拍", event.unified_msg_origin), "换成外套，可以吗？", outfit="coat", request_summary="用户修改条件，需要重新确认"))
                task = current._job_tasks.get(queued["job_id"])
                if task:
                    await asyncio.gather(task, return_exceptions=True)
                install(current)
                return {"ok": result.get("ok"), "job_id": queued["job_id"], "status": current.storage.job(queued["job_id"])["status"], "pending": current.storage.read_session(env["key"])["pending"]["requirements"]["outfit"]}
            raise ValueError("Unknown QA action")

        class SlowProvider(PreviewProvider):
            def __init__(self):
                super().__init__("default", {"model": "offline-preview-image"}, delay=10)

        POST_ROUTES["qa/control"] = "qa_control"
        try:
            install(plugin)
            for index in range(24):
                plugin.storage.append_history({"ok": False, "caption": f"旧记录 {index}"})
            await plugin.initialize()
            process = await asyncio.create_subprocess_exec(node, str(Path(__file__).with_name("console_backend_smoke_0516.cjs")), plugin.webui.url, TOKEN)
            code = await process.wait()
            if code:
                raise RuntimeError(f"Chrome backend smoke exited {code}")
        finally:
            if restart:
                await asyncio.gather(restart, return_exceptions=True)
            await latest[0].terminate()
            POST_ROUTES.pop("qa/control", None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node")
    asyncio.run(run(parser.parse_args().node))
