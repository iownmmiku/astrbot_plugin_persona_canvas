"""Isolated request/persistence checks against real AstrBot SDK sources.

Run in AstrBot's Python with --astrbot-path <source> and optional repeatable
--site-packages <dependencies>. No adapters, models or existing bot data load.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


async def run(isolation):
    from astrbot.core import db_helper
    from astrbot.core.config.default import VERSION as ASTRBOT_VERSION
    from astrbot.core.agent.message import TextPart, UserMessageSegment, dump_messages_with_checkpoints
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.message.components import Plain
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.utils.astrbot_path import get_astrbot_root
    from astrbot_plugin_persona_canvas import main
    from astrbot_plugin_persona_canvas.storage import Storage
    from astrbot_plugin_persona_canvas.companion import apply_state

    assert Path(get_astrbot_root()).resolve() == isolation
    await db_helper.initialize()
    manager = ConversationManager(db_helper)
    umo = "offline:FriendMessage:123"
    history = [{"role": "user", "content": "以前的聊天"}, {"role": "assistant", "content": "以前的回复"}]
    cid = await manager.new_conversation(umo, content=copy.deepcopy(history), persona_id="offline")

    class PersonaManager:
        async def get_persona(self, persona_id):
            return {"persona_id": persona_id, "system_prompt": "原始人格"}

    context = SimpleNamespace(conversation_manager=manager, persona_manager=PersonaManager())
    storage = Storage(isolation / "canvas-storage")
    with patch.object(main, "Storage", return_value=storage):
        plugin = main.PersonaCanvasPlugin(context, {})
    checks = []
    try:
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.session_id, message.message_id = "123", "offline-turn"
        message.sender = MessageMember("123", "offline-user")
        message.message = [Plain("看看自拍")]
        message.message_str = "看看自拍"
        event = AstrMessageEvent("看看自拍", message, PlatformMetadata(name="offline", id="offline", description="Offline SDK check"), "123")
        event.is_at_or_wake_command = True
        assert event.unified_msg_origin == umo
        conversation = await manager.get_conversation(umo, cid)
        permanent, other_temp = TextPart(text="其他插件的持久资料"), TextPart(text="其他插件的临时资料").mark_as_temp()
        req = ProviderRequest(prompt="用户原始发言", system_prompt="固定 system prefix", contexts=copy.deepcopy(history), conversation=conversation, extra_user_content_parts=[permanent, other_temp])
        parts = req.extra_user_content_parts
        await plugin.inject_visual_state(event, req)
        assert req.system_prompt == "固定 system prefix" and req.prompt == "用户原始发言"
        assert req.contexts == history and req.conversation is conversation
        assert req.extra_user_content_parts is parts and parts[:2] == [permanent, other_temp]
        first = parts[-1]
        assert isinstance(first, TextPart) and first.model_dump_for_context()["_no_save"] is True
        assert "当前视觉档案" in first.text and "request_summary" in first.text
        assert "本轮用户消息已通过明确图片请求检查" in first.text
        checks.append("real hook + ProviderRequest: original system/user/history and other content unchanged")

        assembled = await req.assemble_context()
        assert assembled["role"] == "user"
        assert assembled["content"][0]["text"] == req.prompt
        assert assembled["content"][-1]["_no_save"] is True
        assert "当前视觉档案" in assembled["content"][-1]["text"]
        checks.append("real request assembly: visual context reaches current user content with temporary marker")
        user = UserMessageSegment.model_validate(assembled)
        persisted = dump_messages_with_checkpoints([user])
        assert persisted == [{"role": "user", "content": [{"type": "text", "text": "用户原始发言"}, {"type": "text", "text": "其他插件的持久资料"}]}]
        checks.append("real runtime parsing and history serialization remove both temporary blocks and keep permanent content")
        assert json.loads((await manager.get_conversation(umo, cid)).history) == history
        await manager.update_conversation(umo, cid, history=history + persisted + [{"role": "assistant", "content": "正常回复"}])
        saved = json.loads((await manager.get_conversation(umo, cid)).history)
        assert saved[:2] == history and saved[2] == persisted[0]
        assert "当前视觉档案" not in json.dumps(saved, ensure_ascii=False)
        checks.append("real temporary SQLite conversation: prior history retained, current user/reply saved, visual block absent")

        env = await plugin.dialogue.environment(umo)
        session = storage.read_session(env["key"])
        apply_state(session, {"outfit": "new_sdk_dress"}, storage.settings)
        storage.save_session(env["key"], session)
        pending = plugin._save_conditions(env, "看看自拍", "拍远一点可以吗", {"camera": "wide shot"})
        await plugin.inject_visual_state(event, req)
        assert len(parts) == 3 and parts[0] is permanent and parts[1] is other_temp and parts[-1] is not first
        assert req.system_prompt == "固定 system prefix"
        assert "new_sdk_dress" in parts[-1].text and pending["request_id"] in parts[-1].text
        checks.append("real TextPart identity replacement: latest state and pending conditions, one owned block")

        storage.settings["integration"]["mode"] = "compatibility"
        await plugin.inject_visual_state(event, req)
        assert "原生工具支持上下文语义判断" not in parts[-1].text
        assert req.system_prompt == "固定 system prefix"
        checks.append("mode-dependent tool guidance remains transient and leaves system prefix unchanged")
        storage.settings["integration"]["enabled"] = False
        await plugin.inject_visual_state(event, req)
        assert parts == [permanent, other_temp]
        assert req.system_prompt == "固定 system prefix"
        checks.append("disabled integration cleans only the owned block and retains other plugin content")
    finally:
        await plugin.terminate()
        await db_helper.engine.dispose()
    print(json.dumps({"ok": True, "astrbot_version": ASTRBOT_VERSION, "checks": checks, "external_model_verified": False, "external_platform_delivery_verified": False}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--astrbot-path", required=True)
    parser.add_argument("--site-packages", action="append", default=[])
    args = parser.parse_args()
    source = Path(args.astrbot_path).resolve()
    if not (source / "astrbot" / "__init__.py").is_file():
        parser.error("--astrbot-path must contain the real astrbot package")
    sys.dont_write_bytecode = True
    sys.path.extend(str(Path(path).resolve()) for path in args.site_packages)
    sys.path[:0] = [str(source), str(ROOT.parent)]
    # Use the system temp directory so no source-root traversal can encounter
    # SDK-generated data. No existing AstrBot root or user configuration loads.
    with tempfile.TemporaryDirectory(prefix="canvas-transient-sdk-") as directory:
        isolation = Path(directory).resolve()
        os.environ["ASTRBOT_ROOT"] = str(isolation)
        previous = Path.cwd()
        os.chdir(isolation)
        try:
            asyncio.run(run(isolation))
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    main()
