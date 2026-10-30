"""Settings, discovery and state isolation for optional conversation pictures."""
import copy
import unittest

import test_core as fixtures
import test_reply_images as reply_fixtures
from test_comfyui import FIELDS, WORKFLOW, provider as comfy_provider, working_http
from test_core import Event
from canvas_test.active import ActiveScheduler
from canvas_test.storage import Storage


class Settings057Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    async def test_defaults_keep_reply_images_off_and_old_settings_upgrade(self):
        self.assertFalse(self.store.settings["reply_images"]["enabled"])
        self.store._db.execute("UPDATE documents SET payload=json_remove(payload,'$.reply_images') WHERE name='settings'")
        self.store._db.commit()
        reopened = Storage(self.store.root)
        try:
            self.assertFalse(reopened.settings["reply_images"]["enabled"])
            self.assertEqual(reopened.settings["reply_images"]["default_probability"], 20)
        finally:
            reopened.close()

    async def test_group_discovery_does_not_enable_proactive_messages(self):
        scheduler = ActiveScheduler(self.plugin)
        group = Event("聊聊天", "qq:GroupMessage:42")
        group.is_private_chat = lambda: False
        group.get_group_id = lambda: "42"
        scheduler.observe(group)
        target = self.store.targets["items"][0]
        self.assertEqual(target["scope"], "group")
        self.assertFalse(target["reply_image_enabled"])
        self.assertIn("42", target["name"])
        target.update(enabled=True, morning_enabled=True)
        self.assertTrue(scheduler._blocked(target, 99999999999))
        scheduler.observe(group)
        self.assertFalse(target["enabled"])
        self.assertFalse(target["morning_enabled"])

    async def test_chat_probability_and_global_settings_round_trip(self):
        scheduler = ActiveScheduler(self.plugin)
        private = Event("你好")
        scheduler.observe(private)
        group = Event("你好", "qq:GroupMessage:42")
        group.is_private_chat = lambda: False
        scheduler.observe(group)
        for event, probability in ((private, 45.5), (group, 75)):
            await self.plugin.web_target_action({"action": "save", "umo": event.unified_msg_origin, "patch": {"reply_image_enabled": True, "reply_image_probability": probability}})
        await self.plugin.web_save_settings({"reply_images": {"enabled": True, "default_probability": 30, "min_interval_sec": 30, "provider": "default"}})
        reopened = Storage(self.store.root)
        try:
            self.assertEqual(reopened.settings["reply_images"]["default_probability"], 30)
            self.assertEqual([item["reply_image_probability"] for item in reopened.targets["items"]], [45.5, 75])
            self.assertTrue(all(item["reply_image_enabled"] for item in reopened.targets["items"]))
            self.assertFalse(reopened.targets["items"][1]["enabled"])
        finally:
            reopened.close()

    async def test_invalid_probability_and_group_contact_are_rejected(self):
        group = Event("你好", "qq:GroupMessage:42")
        group.is_private_chat = lambda: False
        ActiveScheduler(self.plugin).observe(group)
        for probability in (-1, 101, float("nan"), True):
            with self.subTest(probability=probability):
                with self.assertRaises(ValueError):
                    await self.plugin.web_target_action({"action": "save", "umo": group.unified_msg_origin, "patch": {"reply_image_probability": probability}})
                with self.assertRaises(ValueError):
                    await self.plugin.web_save_settings({"reply_images": {"default_probability": probability}})
        for action in ("toggle", "test"):
            with self.assertRaises(ValueError):
                await self.plugin.web_target_action({"action": action, "umo": group.unified_msg_origin})
        with self.assertRaises(ValueError):
            await self.plugin.web_target_action({"action": "save", "umo": group.unified_msg_origin, "patch": {"enabled": True}})
        self.assertFalse(self.context.sent)

    async def test_reply_picture_leaves_persistent_state_and_pending_unchanged(self):
        self.store.settings["reply_images"]["enabled"] = True
        self.store.targets["items"] = [{"umo": Event().unified_msg_origin, "reply_image_enabled": True}]
        env = await self.plugin.dialogue.environment(Event().unified_msg_origin)
        self.plugin._save_conditions(env, "自拍", "需要远景，可以吗？", {"camera": "wide shot"})
        before = copy.deepcopy(self.store.read_session(env["key"]))
        job = await self.plugin._generate_job(env, "smiling portrait", "persona", {"outfit": "red dress"}, source="reply_image", is_admin=True)
        self.assertFalse(job["state_committed"])
        self.assertEqual(self.store.read_session(env["key"]), before)
        self.assertEqual(job["status"], "succeeded")

    async def test_unknown_reply_provider_rejected_without_saving_settings(self):
        before = copy.deepcopy(self.store.settings)
        with self.assertRaises(ValueError):
            await self.plugin.web_save_settings({"reply_images": {"provider": "missing"}})
        self.assertEqual(self.store.settings, before)

    async def test_comfy_workflow_save_preserves_redacted_node_credentials(self):
        workflow = copy.deepcopy(WORKFLOW)
        workflow["20"] = {"class_type": "CustomNode", "inputs": {"api_key": "local-node-secret-123", "name": "test"}}
        config = {"name": "local", "kind": "comfyui", "endpoint": "http://127.0.0.1:8188", "workflow": workflow, "workflow_fields": FIELDS}
        saved = await self.plugin.web_save_provider(config)
        self.assertNotIn("local-node-secret-123", str(saved))
        self.assertTrue(saved["workflow"]["20"]["inputs"]["has_api_key"])
        await self.plugin.web_save_provider(saved)
        inputs = self.store.settings["providers"]["local"]["workflow"]["20"]["inputs"]
        self.assertEqual(inputs["api_key"], "local-node-secret-123")
        self.assertNotIn("has_api_key", inputs)
        # A new imported value or an explicitly empty value takes precedence.
        saved["workflow"]["20"]["inputs"]["api_key"] = ""
        await self.plugin.web_save_provider(saved)
        self.assertEqual(self.store.settings["providers"]["local"]["workflow"]["20"]["inputs"]["api_key"], "")
        empty_editor = self.store.safe_settings()["providers"]["local"]
        self.assertFalse(empty_editor["workflow"]["20"]["inputs"]["has_api_key"])
        await self.plugin.web_save_provider(empty_editor)
        self.assertEqual(self.store.settings["providers"]["local"]["workflow"]["20"]["inputs"]["api_key"], "")

    async def test_automatic_reply_picture_cannot_use_manual_only_provider(self):
        env = await self.plugin.dialogue.environment(Event().unified_msg_origin)
        self.image_provider.capabilities = copy.deepcopy(self.image_provider.capabilities)
        self.image_provider.capabilities.automated_generation = False
        with self.assertRaisesRegex(ValueError, "对话随机配图"):
            await self.plugin._generate_job(env, "portrait", source="reply_image", is_admin=True)
        self.assertFalse(self.image_provider.calls)

    async def test_comfy_generation_uses_shared_task_and_safe_workflow_summary(self):
        self.store.settings["reply_images"]["enabled"] = True
        self.store.targets["items"] = [{"umo": Event().unified_msg_origin, "reply_image_enabled": True}]
        item = comfy_provider()
        submitted = []
        request = working_http(item)
        async def transport(method, url, body=None, **kwargs):
            if url.endswith("/prompt"):
                submitted.append(copy.deepcopy(body["prompt"]))
            return await request(method, url, body, **kwargs)
        item._http = transport
        self.plugin._provider = lambda _name=None: item
        env = await self.plugin.dialogue.environment(Event().unified_msg_origin)
        job = await self.plugin._generate_job(env, "smiling portrait", raw="今天在海边聊天", source="reply_image", is_admin=True)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["provider"], "local")
        self.assertTrue(job["request_summary"]["submitted"])
        self.assertEqual(job["request_summary"]["workflow_prompt_id"], "task-123")
        self.assertEqual(submitted[0]["3"]["inputs"]["sampler_name"], "euler_ancestral")
        self.assertNotIn("workflow", job["request_summary"])
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        self.assertFalse(job["state_committed"])

    async def test_failed_reply_picture_retry_preserves_state_isolation(self):
        event = reply_fixtures.ReplyImageTests.event(self)
        reply_fixtures.ReplyImageTests.enable(self, event)
        env = await self.plugin.dialogue.environment(event.unified_msg_origin)
        before = copy.deepcopy(self.store.read_session(env["key"]))
        self.image_provider.error = "offline generation failure"
        await self.plugin.reply_images.decorate(event)
        old = self.store.recent_jobs()[0]
        self.assertEqual(old["status"], "failed")
        self.image_provider.error = None
        result = await self.plugin.web_retry_job({"id": old["id"]})
        while self.plugin._tasks:
            await fixtures.asyncio.gather(*list(self.plugin._tasks), return_exceptions=True)
        retried = self.store.job(result["job_id"])
        self.assertEqual(retried["status"], "succeeded")
        self.assertEqual(retried["source"], "reply_image")
        self.assertFalse(retried["state_committed"])
        self.assertEqual(self.store.read_session(env["key"]), before)
        self.assertEqual(len(event.result.chain), 1)

    async def test_reply_picture_provider_initialisation_failure_can_be_retried(self):
        event = reply_fixtures.ReplyImageTests.event(self)
        reply_fixtures.ReplyImageTests.enable(self, event)
        def unavailable(_name=None):
            raise ValueError("local workflow not configured")
        self.plugin._provider = unavailable
        await self.plugin.reply_images.decorate(event)
        old = self.store.recent_jobs()[0]
        self.assertEqual(old["status"], "failed")
        self.assertEqual(old["failure_stage"], "provider")
        self.plugin._provider = lambda _name=None: self.image_provider
        result = await self.plugin.web_retry_job({"id": old["id"]})
        while self.plugin._tasks:
            await fixtures.asyncio.gather(*list(self.plugin._tasks), return_exceptions=True)
        retried = self.store.job(result["job_id"])
        self.assertEqual(retried["status"], "succeeded")
        self.assertFalse(retried["state_committed"])
