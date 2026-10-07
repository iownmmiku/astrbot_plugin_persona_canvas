"""Offline integration tests. Platform, LLM and image API IO are mocked."""
import ast
import asyncio
import importlib.util
import io
import json
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

class Chain:
    def __init__(self, chain=None, **_kwargs):
        self.chain = list(chain or [])
    def message(self, text):
        self.chain.append(("text", text))
        return self
    def file_image(self, path):
        if not Path(path).is_file():
            raise ValueError("Missing image file")
        self.chain.append(("image", path))
        return self

def decorator(*_args, **_kwargs):
    return lambda fn: fn

class Star:
    def __init__(self, context):
        self.context = context

astro = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("canvas-tests")
event_module = types.ModuleType("astrbot.api.event")
event_module.AstrMessageEvent = object
event_module.MessageChain = Chain
event_module.filter = types.SimpleNamespace(command=decorator, llm_tool=decorator, on_llm_request=decorator, on_llm_response=decorator, event_message_type=decorator, EventMessageType=types.SimpleNamespace(ALL=1, PRIVATE_MESSAGE=2))
star_module = types.ModuleType("astrbot.api.star")
star_module.Context, star_module.Star, star_module.register = object, Star, decorator
components_module = types.ModuleType("astrbot.api.message_components")
components_module.Image = type("PlatformImage", (), {})
for name, module in {"astrbot": astro, "astrbot.api": api, "astrbot.api.event": event_module, "astrbot.api.star": star_module, "astrbot.api.message_components": components_module}.items():
    sys.modules[name] = module
spec = importlib.util.spec_from_file_location("canvas_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules["canvas_test"] = package
spec.loader.exec_module(package)
from canvas_test import main
from canvas_test.active import ActiveScheduler
from canvas_test.intent import photo_request, heuristic_intent
from canvas_test.providers.base import GeneratedImage, ProviderCapabilities
from canvas_test.storage import Storage

def image_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(buffer, "PNG")
    return buffer.getvalue()

class ImageProvider:
    name = "default"
    capabilities = ProviderCapabilities(image_to_image=True)
    def __init__(self):
        self.calls = []
        self.error = None
        self.pause = None
    async def generate(self, positive, negative, *, reference=None, options=None):
        self.calls.append({"positive": positive, "negative": negative, "reference": reference, "options": options})
        if self.pause:
            await self.pause.wait()
        if self.error:
            raise ValueError(self.error)
        return GeneratedImage(image_bytes(), "png", "default", "mock")

class LLM:
    def __init__(self):
        self.calls = []
        self.next = {"decision": "refuse", "reply": "有点害羞，今天不想拍。"}
    def meta(self):
        return types.SimpleNamespace(id="chat", model="mock-chat", type="openai")
    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(completion_text=json.dumps(self.next, ensure_ascii=False))

class ConversationManager:
    def __init__(self):
        self.cid = "conversation-1"
        self.history = [{"role": "user", "content": "你刚才说白裙很好看"}, {"role": "assistant", "content": "是呀"}]
    async def get_curr_conversation_id(self, umo):
        return self.cid
    async def get_conversation(self, umo, cid):
        return types.SimpleNamespace(cid=cid, persona_id="shy", history=json.dumps(self.history))
    async def add_message_pair(self, cid, user, assistant):
        self.history.extend([user, assistant])

class PersonaManager:
    personas = [types.SimpleNamespace(persona_id="shy")]
    async def get_persona(self, persona_id):
        return types.SimpleNamespace(persona_id=persona_id, system_prompt="你是害羞的角色，不会轻易答应照片请求。")
    async def get_default_persona_v3(self, umo=None):
        return {"name": "shy", "prompt": "你是害羞的角色"}

class Context:
    def __init__(self):
        self.llm = LLM()
        self.conversation_manager = ConversationManager()
        self.persona_manager = PersonaManager()
        self.sent = []
        self.routes = []
    def get_using_provider(self, umo=None):
        return self.llm
    def get_provider_by_id(self, id):
        return self.llm if id == "chat" else None
    def get_all_providers(self):
        return [self.llm]
    def register_web_api(self, *args):
        self.routes.append(args)
    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True

class Event:
    def __init__(self, text="给我拍张泳装照片", umo="qq:FriendMessage:123"):
        self.message_str, self.unified_msg_origin = text, umo
        self.is_at_or_wake_command = True
        self.extras, self.sent = {}, []
        self.call_llm = True
    def is_private_chat(self):
        return True
    def is_admin(self):
        return False
    def get_platform_id(self):
        return "qq"
    def get_sender_id(self):
        return "123"
    def get_messages(self):
        return []
    def set_extra(self, key, value):
        self.extras[key] = value
    def get_extra(self, key, default=None):
        return self.extras.get(key, default)
    def should_call_llm(self, value):
        self.call_llm = value
    def plain_result(self, text):
        return Chain().message(text)
    async def send(self, chain):
        self.sent.append(chain)

class RequestTests(unittest.TestCase):
    def test_short_view_requests_are_eligible(self):
        for text in ("看看自拍", "看下你的自拍", "看一下你的照片吧", "看看你今天的自拍", "想看看自拍", "能让我看看自拍吗？", "可以看一眼你的自拍吗？"):
            with self.subTest(text=text):
                self.assertTrue(photo_request(text))

    def test_selfie_mentions_are_not_view_requests(self):
        for text in ("自拍", "你喜欢自拍吗？", "你喜欢看看自拍吗？", "我在看看自拍", "看看自拍是什么", "看看自拍的技巧", "不要看看自拍", "她说“看看自拍”", "“看看自拍”", "```看看自拍```", "假如看看自拍会怎样", "讨论看看自拍"):
            with self.subTest(text=text):
                self.assertFalse(photo_request(text))

    def test_no_accidental_generation(self):
        for text in ("我不想自拍", "你穿泳装会害羞吗？", "今天在大海边散步很舒服", "你平时喜欢什么姿势？", "比如‘拍一张你的照片’", "讨论给我拍一张照片", "昨天我拍了一张照片", "不要给我发照片了"):
            with self.subTest(text=text):
                self.assertFalse(photo_request(text))
        for text in ("给我拍一张你的自拍", "给我画一只猫", "给我画一片大海", "把刚才的图片背景换成夜景"):
            self.assertTrue(photo_request(text), text)
        self.assertEqual(heuristic_intent("给我画一只猫").mode, "scene")

    def test_confirmation_expires_and_requires_context(self):
        self.assertFalse(photo_request("好"))
        self.assertTrue(photo_request("好，就按你说的拍", {"request_kind": "photo", "expires_at": time.time() + 60}))
        self.assertFalse(photo_request("好", {"request_kind": "photo", "expires_at": time.time() - 60}))
        self.assertFalse(photo_request("好", {"request_kind": "state", "expires_at": time.time() + 60}))
        self.assertTrue(photo_request("再来一张", {"last_image": "one.png"}))

    def test_all_python_sources_parse(self):
        for path in ROOT.rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"))

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = Context()
        self.store = Storage(Path(self.temp.name))
        with patch.object(main, "Storage", return_value=self.store):
            self.plugin = main.PersonaCanvasPlugin(self.context, {})
        self.image_provider = ImageProvider()
        self.plugin._provider = lambda name=None: self.image_provider
        self.store.settings["moderation"]["min_interval_sec"] = 0

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def consume(self, generator):
        return [item async for item in generator]

    async def test_command_refusal_uses_persona_history_and_never_generates(self):
        event = Event("/生图 泳装自拍")
        await self.consume(self.plugin.command_generate(event, "泳装自拍"))
        self.assertEqual(len(self.context.llm.calls), 1)
        call = self.context.llm.calls[0]
        self.assertIn("害羞", call["system_prompt"])
        self.assertEqual(call["contexts"], self.context.conversation_manager.history[:-2])
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.store.recent_jobs())
        self.assertFalse(event.call_llm)
        self.assertIn("害羞", event.sent[0].chain[0][1])

    async def test_role_conditions_then_confirmation_can_generate(self):
        self.store.settings["integration"]["mode"] = "compatibility"
        self.context.llm.next = {"decision": "ask", "reply": "拍远一点，可以吗？"}
        event = Event()
        await self.consume(self.plugin.natural_route(event))
        self.assertFalse(self.image_provider.calls)
        env = await self.plugin.dialogue.environment(event.unified_msg_origin)
        self.assertIn("拍远", env["session"]["pending"]["conditions"])
        self.context.llm.next = {"decision": "photo", "reply": "那我就拍远一点啦。", "prompt": "white swimsuit, distant camera, shy expression", "state_patch": {"outfit": "white swimsuit", "expression": "shy"}}
        confirmation = Event("好，就按你说的拍")
        await self.consume(self.plugin.natural_route(confirmation))
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(self.store.recent_jobs()[0]["status"], "sent")
        session = self.store.session(env["key"])
        self.assertEqual(session["state"]["outfit"], "white swimsuit")
        self.assertFalse(session["pending"])
        self.assertEqual(len(confirmation.sent[0].chain), 2)

    async def test_native_tool_blocks_discussion_and_repeated_call(self):
        denied = json.loads(await self.plugin.tool_photo(Event("你穿泳装会害羞吗？"), "swimsuit"))
        self.assertFalse(denied["ok"])
        self.assertFalse(self.image_provider.calls)
        event = Event("给我拍一张你的自拍")
        result = json.loads(await self.plugin.tool_photo(event, "white dress, shy", outfit="white dress"))
        self.assertTrue(result["ok"])
        repeated = json.loads(await self.plugin.tool_photo(event, "again"))
        self.assertFalse(repeated["ok"])
        await asyncio.gather(*list(self.plugin._tasks))
        self.assertEqual(len(self.image_provider.calls), 1)

    async def test_private_short_selfie_native_tool_queues_and_sends_image(self):
        event = Event("看看自拍")
        event.is_at_or_wake_command = False
        result = json.loads(await self.plugin.tool_photo(event, "portrait, white dress", caption="给你看看。"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "queued")
        self.assertFalse(result["image_sent"])
        await asyncio.gather(*list(self.plugin._tasks))
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(self.store.job(result["job_id"])["status"], "sent")
        self.assertEqual([kind for kind, _ in event.sent[0].chain], ["text", "image"])

    async def test_short_selfie_compatibility_respects_refusal_and_consent(self):
        self.store.settings["integration"]["mode"] = "compatibility"
        refusal = Event("看看自拍")
        refusal.is_at_or_wake_command = False
        await self.consume(self.plugin.natural_route(refusal))
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertFalse(self.image_provider.calls)
        self.assertFalse(self.store.recent_jobs())
        self.context.llm.next = {"decision": "photo", "reply": "好呀，给你拍一张。", "prompt": "portrait, white dress"}
        agreed = Event("看下你的自拍")
        agreed.is_at_or_wake_command = False
        await self.consume(self.plugin.natural_route(agreed))
        self.assertEqual(len(self.context.llm.calls), 2)
        self.assertEqual(len(self.image_provider.calls), 1)
        self.assertEqual(self.store.recent_jobs()[0]["status"], "sent")

    async def test_native_delivery_failure_stays_uncertain_and_replay_does_not_generate(self):
        event = Event("给我拍一张你的自拍")
        event.message_obj = types.SimpleNamespace(message_id="native-123")
        async def failed_send(_chain):
            raise ConnectionError("adapter connection lost")
        event.send = failed_send
        result = json.loads(await self.plugin.tool_photo(event, "white dress"))
        await asyncio.gather(*list(self.plugin._tasks))
        job = self.store.job(result["job_id"])
        self.assertEqual(job["status"], "uncertain")
        self.assertEqual(self.store.delivery(job["delivery_key"])["status"], "uncertain")
        replay = Event(event.message_str)
        replay.message_obj = event.message_obj
        self.assertFalse(json.loads(await self.plugin.tool_photo(replay, "white dress"))["ok"])
        self.assertEqual(len(self.image_provider.calls), 1)
        with self.assertRaises(ValueError):
            await self.plugin.web_retry_job({"id": job["id"]})

    async def test_failed_generation_keeps_state_and_refunds_daily_quota(self):
        self.image_provider.error = "failure"
        event = Event("给我拍一张你的自拍")
        before = await self.plugin.dialogue.environment(event.unified_msg_origin)
        result = json.loads(await self.plugin.tool_photo(event, "coat", outfit="coat"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["image_sent"])
        await asyncio.gather(*list(self.plugin._tasks))
        self.assertEqual(self.store.session(before["key"])["state"], before["session"]["state"])
        self.assertEqual(self.store.recent_jobs()[0]["status"], "failed")
        self.assertFalse(any(kind == "image" for chain in event.sent for kind, _value in chain.chain))
        self.assertTrue(self.store.reserve_quota("qq:123", {"daily_limit": 1, "min_interval_sec": 0, "max_concurrency": 1})[0])
        self.store.finish_quota("qq:123")

    async def test_state_updates_do_not_generate_and_sessions_are_isolated(self):
        event = Event("换成白色连衣裙")
        result = json.loads(await self.plugin.tool_state(event, outfit="white dress"))
        self.assertTrue(result["ok"])
        self.assertFalse(self.image_provider.calls)
        other = await self.plugin.dialogue.environment("qq:FriendMessage:456")
        self.assertEqual(other["session"]["state"]["outfit"], "")
        self.context.conversation_manager.cid = "conversation-2"
        fresh = await self.plugin.dialogue.environment(event.unified_msg_origin)
        self.assertEqual(fresh["session"]["state"]["outfit"], "")

    async def test_hook_preserves_astrbot_system_and_pending_refusal(self):
        event = Event()
        req = types.SimpleNamespace(system_prompt="原来的AstrBot人格", conversation=None)
        await self.plugin.inject_visual_state(event, req)
        self.assertTrue(req.system_prompt.startswith("原来的AstrBot人格"))
        await self.plugin.remember_conditions(event, types.SimpleNamespace(completion_text="远一点可以吗？"))
        env = await self.plugin.dialogue.environment(event.unified_msg_origin)
        self.assertTrue(env["session"]["pending"])
        refusal = Event("不要发照片了")
        await self.plugin.remember_conditions(refusal, types.SimpleNamespace(completion_text="好，陪你聊天"))
        self.assertFalse(self.store.session(env["key"])["pending"])

    async def test_web_settings_are_hot_and_never_return_keys(self):
        self.store.settings["providers"]["default"]["api_key"] = "never-expose-this"
        saved = await self.plugin.web_save_settings({"moderation": {"daily_limit": 12}, "integration": {"mode": "compatibility"}})
        self.assertEqual(self.plugin.moderation.settings["moderation"]["daily_limit"], 12)
        self.assertNotIn("never-expose-this", json.dumps(saved))
        self.plugin._apply_plugin_config()
        self.assertEqual(self.store.settings["integration"]["mode"], "compatibility")
        with self.assertRaises(ValueError):
            await self.plugin.web_save_settings({"generation": {"max_concurrency": 0}})

    async def test_web_role_refusal_and_dry_run_never_generate(self):
        decision = await self.plugin.web_generate({"text": "拍一张泳装照片", "mode": "persona"})
        self.assertEqual(decision["decision"], "refuse")
        self.assertFalse(self.store.recent_jobs())
        self.context.llm.next = {"decision": "photo", "reply": "好呀", "prompt": "white dress"}
        simulated = await self.plugin.web_simulate({"text": "给我拍一张自拍"})
        self.assertFalse(simulated["executed"])
        self.assertFalse(self.store.recent_jobs())

    async def test_web_job_options_are_local_and_reference_is_sent(self):
        self.context.llm.next = {"decision": "edit", "reply": "换成夜景啦", "prompt": "night background"}
        ref = await self.plugin.web_upload_reference(image_bytes(), "one.png")
        defaults = dict(self.store.settings["generation"])
        queued = await self.plugin.web_generate({"text": "把背景改成夜景", "mode": "edit", "width": 1024, "height": 1024, "reference_asset": ref["asset"]})
        await asyncio.gather(*list(self.plugin._tasks))
        job = await self.plugin.web_job(queued["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(self.image_provider.calls[0]["reference"], image_bytes())
        self.assertEqual(self.image_provider.calls[0]["options"]["width"], 1024)
        self.assertEqual(self.store.settings["generation"], defaults)
        self.assertEqual(job["asset"], ref["asset"])

    async def test_retry_preserves_explicit_parameters_without_promoting_defaults(self):
        self.context.llm.next = {"decision": "photo", "reply": "好呀", "prompt": "white dress"}
        self.image_provider.error = "temporary image API failure"
        original = await self.plugin.web_generate({"text": "拍张白裙照片", "width": 1024})
        await asyncio.gather(*list(self.plugin._tasks))
        self.image_provider.error = None
        retry = await self.plugin.web_retry_job({"id": original["job_id"]})
        await asyncio.gather(*list(self.plugin._tasks))
        self.assertEqual(self.image_provider.calls[-1]["options"]["_explicit"], ["width"])
        self.assertEqual(self.store.job(retry["job_id"])["status"], "succeeded")

    async def test_decision_timeout_uses_selected_provider_or_current_dialogue_setting(self):
        env = await self.plugin.dialogue.environment("qq:FriendMessage:123")
        self.store.settings["dialogue"]["timeout_sec"] = 17
        self.store.settings["llm"].update(provider_id="chat", timeout_sec=23)
        real_wait = asyncio.wait_for
        with patch("canvas_test.dialogue.asyncio.wait_for", wraps=real_wait) as wait:
            await self.plugin.dialogue.decide(env, "拍张照片")
            self.assertEqual(wait.call_args.args[1], 23)
        self.store.settings["llm"]["provider_id"] = ""
        with patch("canvas_test.dialogue.asyncio.wait_for", wraps=real_wait) as wait:
            await self.plugin.dialogue.decide(env, "拍张照片")
            self.assertEqual(wait.call_args.args[1], 17)

    async def test_invalid_legacy_provider_can_be_repaired_through_console(self):
        self.plugin._provider = types.MethodType(main.PersonaCanvasPlugin._provider, self.plugin)
        self.store.settings["providers"]["default"]["kind"] = "unknown_legacy_kind"
        state = await self.plugin.web_state()
        rows = await self.plugin.web_providers()
        self.assertTrue(state["integration"]["provider_error"])
        self.assertFalse(state["capabilities"]["text_to_image"])
        self.assertTrue(rows["items"][0]["error"])

    async def test_concurrent_state_change_is_not_overwritten_by_older_photo(self):
        self.image_provider.pause = asyncio.Event()
        event = Event("给我拍一张你的自拍")
        running = asyncio.create_task(self.plugin.tool_photo(event, "white dress", outfit="white dress"))
        while not self.image_provider.calls:
            await asyncio.sleep(0)
        env = await self.plugin.dialogue.environment(event.unified_msg_origin)
        session = self.store.session(env["key"])
        session["state"]["outfit"] = "new coat"
        self.store.save_session(env["key"], session)
        self.image_provider.pause.set()
        self.assertTrue(json.loads(await running)["ok"])
        await asyncio.gather(*list(self.plugin._tasks))
        self.assertEqual(self.store.session(env["key"])["state"]["outfit"], "new coat")
        self.assertFalse(self.store.recent_jobs()[0]["state_committed"])

    async def test_scheduler_respects_recent_chat_and_role_skip(self):
        scheduler = ActiveScheduler(self.plugin)
        event = Event()
        scheduler.observe(event)
        target = self.store.targets["items"][0]
        target["enabled"] = True
        self.store.settings["active"]["enabled"] = True
        scheduler._in_window = lambda *args: True
        await scheduler.tick()
        self.assertFalse(self.context.llm.calls)
        target["last_inbound"] = time.time() - 10000
        self.context.llm.next = {"decision": "skip", "reply": ""}
        await scheduler.tick()
        self.assertEqual(len(self.context.llm.calls), 1)
        await scheduler.tick()
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertFalse(self.context.sent)
        self.assertFalse(self.image_provider.calls)

    async def test_morning_uses_llm_and_sent_marker_prevents_repeats(self):
        scheduler = ActiveScheduler(self.plugin)
        scheduler.observe(Event())
        target = self.store.targets["items"][0]
        target.update(enabled=True, last_inbound=time.time() - 10000)
        self.store.settings["good_morning"]["enabled"] = True
        scheduler._in_window = lambda *args: True
        self.context.llm.next = {"decision": "chat", "reply": "早安，昨晚睡好了吗？"}
        await scheduler.tick()
        self.assertEqual(len(self.context.sent), 1)
        self.assertTrue(target["morning_date"])
        target["last_active"] = 0
        await scheduler.tick()
        self.assertEqual(len(self.context.sent), 1)
        target["silent_until"] = time.time() + 10000
        self.assertTrue(scheduler._blocked(target, time.time()))

if __name__ == "__main__":
    unittest.main()
