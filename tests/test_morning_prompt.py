"""Offline prompt regression: inspect requests, without making model calls."""
import tempfile
import unittest
from pathlib import Path

from test_core import Context, Storage
from canvas_test.dialogue import Dialogue


class MorningPromptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name))
        self.addCleanup(self.store.close)
        self.context = Context()
        self.dialogue = Dialogue(self.context, self.store)
        self.env = await self.dialogue.environment("qq:FriendMessage:123")

    async def test_morning_without_topic_can_greet_using_current_persona(self):
        self.env["contexts"] = []
        self.context.llm.next = {"decision": "chat", "reply": "早安，今天也慢慢来吧。"}
        result = await self.dialogue.decide(self.env, "当前是早安机会，目标不允许图片。", proactive=True, event_kind="早安")
        request = self.context.llm.calls[-1]
        self.assertEqual(request["contexts"], [])
        self.assertIn(self.env["system"], request["system_prompt"])
        self.assertIn("不依赖未完话题", request["system_prompt"])
        self.assertIn("优先选择 chat", request["system_prompt"])
        self.assertIn("你愿意且目标允许图片", request["system_prompt"])
        self.assertIn("不强制拍照，不绕过角色意愿", request["system_prompt"])
        self.assertEqual(result["decision"], "chat")
        self.assertEqual(result["reply"], self.context.llm.next["reply"])

    async def test_morning_distinguishes_temporary_skip_and_daily_refusal(self):
        for decision in ("skip", "refuse"):
            with self.subTest(decision=decision):
                self.context.llm.next = {"decision": decision, "reply": "今天先不打扰了。" if decision == "refuse" else ""}
                result = await self.dialogue.decide(self.env, "早安机会", proactive=True, event_kind="早安")
                system = self.context.llm.calls[-1]["system_prompt"]
                self.assertIn("暂时不适合联系，返回 skip", system)
                self.assertIn("用户已要求安静、不再打扰，返回 refuse，当天不再尝试", system)
                self.assertEqual(result["decision"], decision)

    async def test_ordinary_proactive_prompt_keeps_original_behavior(self):
        self.context.llm.next = {"decision": "skip", "reply": ""}
        result = await self.dialogue.decide(self.env, "没有合适话题时选择skip", proactive=True, event_kind="日常")
        request = self.context.llm.calls[-1]
        self.assertIn("日常主动联系机会", request["system_prompt"])
        self.assertNotIn("这是管理员开启的每日早安", request["system_prompt"])
        self.assertTrue(request["prompt"].startswith("没有合适话题时选择skip"))
        self.assertIn("visual_profile", request["prompt"])
        self.assertEqual(result["decision"], "skip")

    async def test_non_proactive_chat_does_not_receive_morning_instruction(self):
        self.context.llm.next = {"decision": "refuse", "reply": "今天不想拍照。"}
        result = await self.dialogue.decide(self.env, "给我看看自拍", event_kind="早安")
        system = self.context.llm.calls[-1]["system_prompt"]
        self.assertNotIn("这是管理员开启的每日早安", system)
        self.assertEqual(result["decision"], "refuse")
        self.assertEqual(result["reply"], "今天不想拍照。")

    async def test_only_morning_can_silently_refuse_contact(self):
        self.context.llm.next = {"decision": "refuse", "reply": ""}
        result = await self.dialogue.decide(self.env, "早安机会", proactive=True, event_kind="早安")
        self.assertEqual(result["decision"], "refuse")
        self.assertEqual(result["reply"], "")
        for kwargs in ({}, {"proactive": True, "event_kind": "日常"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.dialogue.decide(self.env, "给我看看自拍", **kwargs)
