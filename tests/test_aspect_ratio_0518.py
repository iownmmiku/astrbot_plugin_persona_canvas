"""0.5.18: the role picks the aspect ratio, settings pick a resolution tier."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

import test_core as fixtures
from test_core import Event
from canvas_test import common
from canvas_test.intent import validated_decision
from canvas_test.providers.base import provider_from_config
from canvas_test.storage import DEFAULT_SETTINGS, Storage


class CanvasHelperTests(unittest.TestCase):
    def test_tiers_map_ratios_to_bounded_multiples_of_64(self):
        self.assertEqual(common.canvas_size("2:3", "1K"), (832, 1216))
        self.assertEqual(common.canvas_size("1:1", "1K"), (1024, 1024))
        self.assertEqual(common.canvas_size("16:9", "1K"), (1344, 768))
        self.assertEqual(common.canvas_size("2:3", "2K"), (1664, 2496))
        self.assertEqual(common.canvas_size("21:9", "4K"), (4096, 1728))
        for tier in common.RESOLUTIONS:
            for ratio in common.ASPECT_RATIOS:
                width, height = common.canvas_size(ratio, tier)
                self.assertEqual((width % 64, height % 64), (0, 0))
                self.assertLessEqual(max(width, height), 4096)
                w, h = ratio.split(":")
                if f"{h}:{w}" in common.ASPECT_RATIOS:  # Portrait and landscape mirror each other.
                    self.assertEqual(common.canvas_size(f"{h}:{w}", tier), (height, width))

    def test_role_ratios_are_normalised_or_left_to_the_default(self):
        cases = {"16:9": "16:9", "16：9": "16:9", "1920x1080": "16:9", "9/16": "9:16", "3 : 4": "3:4", "2:1": "16:9", "portrait": "", "0:1": "", "": ""}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(common.aspect_ratio(value), expected)

    def test_apply_canvas_prefers_manual_values_then_role_then_default(self):
        default = common.apply_canvas({"resolution": "1K"})
        self.assertEqual((default["aspect_ratio"], default["width"], default["height"]), ("2:3", 832, 1216))
        self.assertNotIn("image_size", default)
        chosen = common.apply_canvas({"resolution": "2K", "width": 832, "height": 1216}, "16:9")
        self.assertEqual((chosen["aspect_ratio"], chosen["width"], chosen["height"], chosen["image_size"]), ("16:9", 2688, 1536, "2K"))
        manual = common.apply_canvas({"resolution": "1K", "aspect_ratio": "1:1", "width": 1000, "_explicit": ["aspect_ratio", "width"]}, "16:9")
        self.assertEqual((manual["aspect_ratio"], manual["width"], manual["height"]), ("1:1", 1000, 1024))
        unknown_tier = common.apply_canvas({"resolution": "8K"}, "nonsense")
        self.assertEqual((unknown_tier["resolution"], unknown_tier["aspect_ratio"]), ("1K", "2:3"))

    def test_gemini_receives_native_ratio_and_tier(self):
        item = provider_from_config("g", {"kind": "gemini", "endpoint": "https://google.example/v1beta", "model": "models/gemini-image", "api_key": "secret"})
        two_k = item._prepare_generation("portrait", "", options=common.apply_canvas({"resolution": "2K"}, "9:16"))
        self.assertEqual(two_k.body["generationConfig"]["imageConfig"], {"aspectRatio": "9:16", "imageSize": "2K"})
        one_k = item._prepare_generation("portrait", "", options=common.apply_canvas({"resolution": "1K"}, "4:3"))
        self.assertEqual(one_k.body["generationConfig"]["imageConfig"], {"aspectRatio": "4:3"})

    def test_role_decision_carries_a_normalised_ratio_only_for_images(self):
        photo = validated_decision(json.dumps({"decision": "photo", "reply": "好", "prompt": "海边", "aspect_ratio": "16：9"}))
        self.assertEqual(photo["aspect_ratio"], "16:9")
        missing = validated_decision(json.dumps({"decision": "photo", "reply": "好", "prompt": "海边"}))
        self.assertEqual(missing["aspect_ratio"], "")
        chat = validated_decision(json.dumps({"decision": "chat", "reply": "聊聊", "aspect_ratio": "16:9"}))
        self.assertEqual(chat["aspect_ratio"], "")

    def test_old_fixed_sizes_become_the_nearest_tier(self):
        self.assertNotIn("width", DEFAULT_SETTINGS["generation"])
        for (width, height), tier in (((832, 1216), "1K"), ((1664, 2432), "2K"), ((4096, 4096), "4K")):
            with self.subTest(size=(width, height)), tempfile.TemporaryDirectory() as directory:
                store = Storage(Path(directory))
                settings = copy.deepcopy(store.settings)
                settings["generation"].update(width=width, height=height)
                settings["generation"].pop("resolution")
                with store._db:
                    store._write_document("settings", settings)
                store.close()
                reopened = Storage(Path(directory))
                try:
                    generation = reopened.settings["generation"]
                    self.assertEqual(generation["resolution"], tier)
                    self.assertNotIn("width", generation)
                    self.assertNotIn("width", reopened._read_document("settings")["generation"])
                finally:
                    reopened.close()


class RoleAspectRatioTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    async def test_photo_tool_ratio_and_tier_reach_the_provider_and_job(self):
        await self.plugin.web_save_settings({"generation": {"resolution": "2K"}})
        event = Event("看看自拍")
        result = json.loads(await self.plugin.tool_photo(event, "seaside, wide view", aspect_ratio="16:9"))
        self.assertTrue(result["ok"], result)
        await asyncio.gather(*list(self.plugin._tasks))
        options = self.image_provider.calls[0]["options"]
        self.assertEqual((options["aspect_ratio"], options["width"], options["height"], options["image_size"]), ("16:9", 2688, 1536, "2K"))
        self.assertEqual(options["_explicit"], [])
        self.assertEqual(self.store.job(result["job_id"])["options"]["aspect_ratio"], "16:9")

    async def test_photo_without_a_ratio_uses_portrait_default(self):
        result = json.loads(await self.plugin.tool_photo(Event("看看自拍"), "portrait"))
        self.assertTrue(result["ok"], result)
        await asyncio.gather(*list(self.plugin._tasks))
        options = self.image_provider.calls[0]["options"]
        self.assertEqual((options["aspect_ratio"], options["width"], options["height"]), ("2:3", 832, 1216))
        self.assertNotIn("image_size", options)

    async def test_retry_keeps_the_original_ratio(self):
        self.image_provider.error = "upstream down"
        result = json.loads(await self.plugin.tool_photo(Event("看看自拍"), "seaside", aspect_ratio="21:9"))
        await asyncio.gather(*list(self.plugin._tasks))
        self.assertEqual(self.store.job(result["job_id"])["status"], "failed")
        self.image_provider.error = None
        retried = await self.plugin.web_retry_job({"id": result["job_id"]})
        await asyncio.gather(*list(self.plugin._tasks))
        options = self.image_provider.calls[-1]["options"]
        self.assertEqual((options["aspect_ratio"], options["width"], options["height"]), ("21:9", 1536, 640))
        self.assertEqual(options["_explicit"], [])
        self.assertEqual(self.store.job(retried["job_id"])["options"]["aspect_ratio"], "21:9")

    async def test_settings_accept_tiers_only(self):
        saved = await self.plugin.web_save_settings({"generation": {"resolution": "4K", "width": 999}})
        self.assertEqual(saved["generation"]["resolution"], "4K")
        self.assertNotIn("width", saved["generation"])
        for tier in ("8K", "1k", 1, None):
            with self.subTest(tier=tier), self.assertRaises(ValueError):
                await self.plugin.web_save_settings({"generation": {"resolution": tier}})


if __name__ == "__main__":
    unittest.main()
