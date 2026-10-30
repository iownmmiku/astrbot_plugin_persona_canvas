"""Conservative greeting and midnight clock regressions; no external IO."""
import unittest

import test_core as fixtures
import test_proactive_time_0512 as clocks
from canvas_test.temporal import temporal_conflicts


class TemporalEdges0515Tests(unittest.TestCase):
    @staticmethod
    def clock(hour=8, minute=30):
        return {"now": f"2026-10-30T{hour:02d}:{minute:02d}:00+08:00", "weekday": "星期五"}

    def test_greeting_substrings_do_not_reject_ordinary_phrases(self):
        for reply in ("下午好好休息，早安！", "愿你今晚安心入睡。", "昨晚安稳睡了一觉，真好。", "等到晚上好好放松。", "她希望每晚安宁。"):
            with self.subTest(reply=reply):
                self.assertEqual(temporal_conflicts(reply, self.clock()), [])

    def test_hypothetical_greetings_are_not_present_greetings(self):
        for reply in ("如果我说晚安，那就是想睡了。", "假如你想说晚安，我会听着。", "你以为我会说晚安。"):
            with self.subTest(reply=reply):
                self.assertEqual(temporal_conflicts(reply, self.clock()), [])

    def test_clear_wrong_greetings_still_conflict(self):
        for reply in ("晚安", "主人晚安！", "晚安啦，早点睡", "下午好，来陪你聊天", "主人，晚上好～"):
            with self.subTest(reply=reply):
                self.assertTrue(temporal_conflicts(reply, self.clock()))
        self.assertTrue(temporal_conflicts("早安！", self.clock(23)))

    def test_greetings_with_direct_address_keep_a_complete_boundary(self):
        for reply in ("晚安主人", "下午好朋友们", "晚上好大家！", "晚安宝贝呀", "晚安你"):
            with self.subTest(reply=reply):
                self.assertTrue(temporal_conflicts(reply, self.clock()))
        for reply in ("下午好好休息", "今晚安心入睡", "下午好朋友们约我见面", "晚上好朋友们会来做客"):
            with self.subTest(reply=reply):
                self.assertEqual(temporal_conflicts(reply, self.clock()), [])

    def test_twelve_hour_clock_uses_daypart_across_midnight(self):
        cases = ((0, 0, "现在深夜十二点了"), (0, 30, "现在十二点半"), (12, 30, "现在十二点半"), (1, 0, "现在深夜一点了"), (2, 15, "现在深夜两点一刻"), (23, 0, "现在深夜十一点了"), (0, 0, "现在凌晨十二点了"), (13, 0, "现在中午一点了"), (23, 30, "现在深夜23:30"))
        for hour, minute, reply in cases:
            with self.subTest(reply=reply, hour=hour):
                self.assertEqual(temporal_conflicts(reply, self.clock(hour, minute)), [])
        for hour, minute, reply in ((1, 0, "现在深夜两点"), (23, 0, "现在深夜一点"), (13, 0, "现在深夜一点"), (0, 0, "现在中午十二点"), (8, 30, "现在十二点半"), (23, 0, "现在深夜六点"), (8, 30, "现在25:30"), (8, 30, "现在08:75")):
            with self.subTest(reply=reply, hour=hour):
                self.assertTrue(temporal_conflicts(reply, self.clock(hour, minute)))


class TemporalDelivery0515Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimeTests.asyncSetUp
    asyncTearDown = fixtures.RuntimeTests.asyncTearDown
    setup_clock = clocks.ProactiveTime0512Tests.setup_clock
    deliver = clocks.ProactiveTime0512Tests.deliver

    async def test_compound_phrase_morning_sends_with_single_llm_budget(self):
        self.setup_clock()
        self.store.settings["proactive_budget"]["llm"] = 1
        self.context.llm.next = {"decision": "chat", "reply": "下午好好休息，早安！"}
        await self.deliver()
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.store.delivery(self.key)["status"], "sent")
        self.assertEqual(self.store.delivery(self.key)["time_review"]["status"], "ok")

    async def test_after_midnight_time_sends_without_paid_correction(self):
        self.setup_clock("active", hour=1, minute=0)
        self.store.settings["proactive_budget"]["llm"] = 1
        self.context.llm.next = {"decision": "chat", "reply": "现在深夜一点了，晚安。"}
        await self.deliver()
        self.assertEqual(len(self.context.llm.calls), 1)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.store.delivery(self.key)["status"], "sent")
        self.assertEqual(self.store.budget_summary(self.store.settings["proactive_budget"])["llm"]["used"], 1)
