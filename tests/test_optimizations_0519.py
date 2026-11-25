"""0.5.19: lighter per-turn context, fewer reply-image writes, gpt-image-2 sizing."""

import time
import types
import unittest
from unittest.mock import patch

import test_core as fixtures
import test_reply_images as reply_fixtures
from test_core import Event
from astrbot.api.message_components import Image
from canvas_test import common, main
from canvas_test.intent import BRIEF_RULES, DECISION_RULES, STATE_ONLY_NOTE
from canvas_test.providers.base import ProviderError, provider_from_config


class VisualContextTiers(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    async def context_for(self, event):
        req = types.SimpleNamespace(system_prompt="原系统提示", prompt=event.message_str, contexts=[], conversation=None, extra_user_content_parts=[])
        await self.plugin.inject_visual_state(event, req)
        self.assertEqual(req.system_prompt, "原系统提示")
        return req.extra_user_content_parts[-1].text

    async def test_unaddressed_turns_get_only_the_current_state(self):
        event = Event("今天好热", "qq:GroupMessage:456")
        event.is_private_chat = lambda: False
        event.is_at_or_wake_command = False
        text = await self.context_for(event)
        self.assertIn(STATE_ONLY_NOTE, text)
        self.assertNotIn("persona_canvas_photo", text)
        self.assertLess(len(text), 400)

    async def test_ordinary_chat_gets_brief_rules_and_image_topics_get_full_rules(self):
        brief = await self.context_for(Event("今天吃了什么"))
        self.assertIn(BRIEF_RULES.strip().splitlines()[0], brief)
        self.assertIn("request_summary", brief)
        self.assertNotIn(DECISION_RULES.strip().splitlines()[0], brief)
        self.assertNotIn("photo_records", brief)
        for text in ("你今天穿的什么", "让我看看你", "刚才那张照片呢"):
            with self.subTest(text=text):
                self.assertIn(DECISION_RULES, await self.context_for(Event(text)))

    async def test_attached_image_and_recent_photo_keep_full_rules(self):
        event = Event("这个怎么样")
        event.get_messages = lambda: [Image()]
        self.assertIn(DECISION_RULES, await self.context_for(event))
        plain = Event("今天吃了什么")
        env = await self.plugin.dialogue.environment(plain.unified_msg_origin)
        self.store.record_action({"kind": "photo", "session_key": env["key"], "persona_id": env["persona"]["id"], "status": "sent", "summary": "seaside"}, "photo:recent")
        self.assertIn(DECISION_RULES, await self.context_for(plain))
        later = time.time() + main.RECENT_PHOTO_SEC + 60
        with patch("canvas_test.dialogue.time.time", return_value=later):
            self.assertNotIn(DECISION_RULES, await self.context_for(plain))


class ReplyImageWrites(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    event = reply_fixtures.ReplyImageTests.event
    enable = reply_fixtures.ReplyImageTests.enable

    def deliveries(self):
        return self.store._db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]

    async def test_probability_miss_and_cooldown_write_nothing(self):
        event = self.event()
        self.enable(event, probability=20)
        with patch("canvas_test.reply_images.random.random", return_value=0.8):
            await self.plugin.reply_images.decorate(event)
        self.assertEqual(self.deliveries(), 0)
        cooling = self.event(identifier="m-2")
        self.store.settings["reply_images"]["min_interval_sec"] = 600
        self.store.targets["items"][0]["reply_image_last_attempt"] = time.time()
        with patch("canvas_test.reply_images.random.random") as draw:
            await self.plugin.reply_images.decorate(cooling)
        draw.assert_not_called()
        self.assertEqual(self.deliveries(), 0)
        self.assertFalse(self.context.llm.calls)

    async def test_hit_still_records_the_delivery(self):
        event = self.event(identifier="m-3")
        self.enable(event, probability=100)
        await self.plugin.reply_images.decorate(event)
        self.assertEqual(self.deliveries(), 1)


class GptImage2Sizing(unittest.TestCase):
    def setUp(self):
        self.item = provider_from_config("o", {"kind": "openai", "endpoint": "https://api.example.com/v1", "model": "gpt-image-2"})

    def size(self, ratio, tier, **extra):
        options = common.apply_canvas({"resolution": tier}, ratio)
        options.update(extra)
        options["_explicit"] = set(extra.get("_explicit", ()))
        return self.item._size("gpt-image-2", options)

    def test_large_tiers_shrink_to_the_model_limit_keeping_the_shape(self):
        self.assertEqual(self.size("2:3", "2K"), "1664x2496")
        self.assertEqual(self.size("2:3", "4K"), "2320x3552")
        self.assertEqual(self.size("1:1", "4K"), "2880x2880")
        self.assertEqual(self.size("16:9", "4K"), "3840x2160")
        self.assertEqual(self.size("21:9", "4K"), "3840x1616")
        for ratio in common.ASPECT_RATIOS:
            width, height = map(int, self.size(ratio, "4K").split("x"))
            self.assertEqual((width % 16, height % 16), (0, 0))
            self.assertLessEqual(width * height, 3840 * 2160)
            self.assertLessEqual(max(width, height), 3840)

    def test_explicit_oversize_is_still_rejected(self):
        with self.assertRaises(ProviderError):
            self.size("1:1", "1K", width=4096, height=4096, _explicit=("width", "height"))


if __name__ == "__main__":
    unittest.main()
