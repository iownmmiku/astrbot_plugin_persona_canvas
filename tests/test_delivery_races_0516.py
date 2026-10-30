"""Regressions for identity changes and the final asynchronous delivery gap."""
import asyncio
import copy
import json
import types
import unittest
from datetime import timedelta

import test_core as fixtures
import test_proactive_time_0512 as clocks


class DeliveryCommitRaces0516Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown

    def switch_astrbot_persona(self, persona_id="new-role"):
        async def changed(_umo, cid):
            return types.SimpleNamespace(cid=cid, persona_id=persona_id, history=json.dumps(self.context.conversation_manager.history))
        self.context.conversation_manager.get_conversation = changed

    async def test_persona_switch_during_generation_keeps_old_visual_state_and_only_saves_image(self):
        env = await self.plugin.dialogue.environment(fixtures.Event().unified_msg_origin)
        before = copy.deepcopy(self.store.read_session(env["key"]))
        self.image_provider.pause = asyncio.Event()
        task = asyncio.create_task(self.plugin._generate_job(
            env, "old role portrait", patch={"outfit": "old-role outfit"}, source="chat", is_admin=True,
        ))
        try:
            for _ in range(200):
                if self.image_provider.calls:
                    break
                await asyncio.sleep(.001)
            self.assertTrue(self.image_provider.calls)
            self.switch_astrbot_persona()
        finally:
            self.image_provider.pause.set()
            job = await task

        self.assertFalse(job["state_committed"])
        self.assertEqual(self.store.read_session(env["key"]), before)
        self.assertTrue(self.store.asset(job["asset"]).is_file())
        with self.assertRaisesRegex(fixtures.main.DeliveryRejectedError, "人设|会话"):
            await self.plugin._send_job(job, umo=env["umo"])
        self.assertFalse(self.context.sent)

    async def test_cached_native_state_conditions_and_confirmation_reject_new_persona(self):
        for index, action in enumerate(("state", "conditions", "confirm")):
            with self.subTest(action=action):
                event = fixtures.Event("换上外套" if action == "state" else "看看自拍" if action == "conditions" else "就按你说的拍")
                env = await self.plugin.dialogue.environment(event.unified_msg_origin)
                pending = None
                if action == "confirm":
                    pending = self.plugin._save_conditions(env, "看看自拍", "外套远景，可以吗？", {"outfit": "coat", "camera": "wide shot"})
                event.set_extra("persona_canvas:environment", env)
                before = copy.deepcopy(self.store.read_session(env["key"]))
                self.switch_astrbot_persona("new-role-" + str(index))

                if action == "state":
                    result = await self.plugin.tool_state(event, outfit="old-role coat", request_summary="用户要求换上外套")
                elif action == "conditions":
                    result = await self.plugin.tool_conditions(event, "外套远景，可以吗？", outfit="coat", request_summary="用户想看自拍，我先提出条件")
                else:
                    result = await self.plugin.tool_control(event, "confirm", "用户确认原条件", request_id=pending["request_id"])

                result = json.loads(result)
                self.assertFalse(result["ok"], result)
                self.assertRegex(result["reason"], "人设|会话")
                self.assertEqual(self.store.read_session(env["key"]), before)
                self.assertFalse(event.get_extra("persona_canvas:confirmed_request"))
        self.assertFalse(self.image_provider.calls)


class FinalProactiveGuardRaces0516Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    setup_clock = clocks.ProactiveTime0512Tests.setup_clock
    deliver = clocks.ProactiveTime0512Tests.deliver
    photo = clocks.ProactiveTime0512Tests.photo

    async def test_last_send_identity_await_rechecks_inbound_settings_and_current_clock(self):
        original_identity = self.plugin.dialogue.current_identity
        for change in ("new_inbound", "morning_disabled", "timezone", "date", "period", "window"):
            with self.subTest(change=change):
                self.setup_clock()
                self.key += ":" + change
                self.target.pop("timezone", None)
                self.target.pop("morning_end", None)
                self.target["morning_enabled"] = True
                self.photo()
                previous_sent = len(self.context.sent)
                triggered = []

                async def changed_identity(umo):
                    # This read is inside _send_job, after ActiveScheduler's
                    # final checks and its transition to the sending stage.
                    if (self.store.delivery(self.key) or {}).get("status") == "sending":
                        triggered.append(change)
                        if change == "new_inbound":
                            self.target["last_inbound"] = self.clock[0].timestamp()
                        elif change == "morning_disabled":
                            self.target["morning_enabled"] = False
                        elif change == "timezone":
                            self.target["timezone"] = "UTC"
                        elif change == "date":
                            self.clock[0] += timedelta(days=1)
                        elif change == "period":
                            self.clock[0] += timedelta(hours=4)
                        else:
                            self.target["morning_end"] = "08:00"
                    return await original_identity(umo)

                self.plugin.dialogue.current_identity = changed_identity
                await self.deliver()
                self.assertTrue(triggered, "test must cross the final _send_job identity await")
                self.assertEqual(len(self.context.sent), previous_sent)
                self.assertEqual(self.store.delivery(self.key)["status"], "skipped")
                self.assertFalse(self.store.delivery(self.key).get("retryable"))
                self.assertNotEqual(self.store.delivery(self.key)["status"], "sent")
                job = self.store.recent_jobs()[0]
                self.assertTrue(self.store.asset(job["asset"]).is_file())
                self.assertTrue(job["delivery_blocked"])


if __name__ == "__main__":
    unittest.main()
