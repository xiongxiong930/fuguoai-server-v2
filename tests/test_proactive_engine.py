"""主动对话引擎离线测试（标准库，无网络/无设备依赖）。

覆盖：存储默认/配置、静默时段(跨午夜)、护栏(频率/静默/去重)、
三类触发(时间/沉默/随机)、跨源仲裁、下发+事件+状态回流。

运行：python3 test_proactive_engine.py
"""

import asyncio
import os
import random
import tempfile
import unittest
from unittest import mock

import fuguoai_core.utils.proactive_engine as pe
from fuguoai_core.utils.proactive_engine import (
    ProactiveStore, ProactiveEngine, Candidate, _in_quiet_hours)


class FakeBus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)


class FakeEventBusModule:
    def __init__(self):
        self._bus = FakeBus()

    def get_event_bus(self):
        return self._bus


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestStoreAndConfig(unittest.TestCase):
    def setUp(self):
        ProactiveStore._BASE_DIR = tempfile.mkdtemp()
        self.store = ProactiveStore()

    def test_default_cfg_opt_in_off(self):
        cfg = self.store.get_config("dev:1")
        self.assertFalse(cfg["enabled"])              # 默认关闭，需用户开启
        self.assertEqual(cfg["max_per_day"], 3)
        self.assertEqual(cfg["quiet_hours"], ["22:00", "08:00"])
        self.assertTrue(cfg["triggers"]["time"])
        self.assertFalse(cfg["allow_external"])       # 外接默认关

    def test_set_config_patch(self):
        cfg = self.store.set_config("dev:1", {
            "enabled": True, "max_per_day": 5,
            "quiet_hours": ["23:00", "07:00"],
            "topics": ["music"], "triggers": {"random": False}})
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["max_per_day"], 5)
        self.assertEqual(cfg["quiet_hours"], ["23:00", "07:00"])
        self.assertEqual(cfg["topics"], ["music"])
        self.assertFalse(cfg["triggers"]["random"])
        self.assertTrue(cfg["triggers"]["time"])      # 未改保留


class TestQuietHours(unittest.TestCase):
    def test_wrap_midnight(self):
        # 23:30 在 22:00-08:00 内
        self.assertTrue(_in_quiet_hours(_mk(23, 30), ["22:00", "08:00"]))
        # 07:30 在内
        self.assertTrue(_in_quiet_hours(_mk(7, 30), ["22:00", "08:00"]))
        # 12:00 不在内
        self.assertFalse(_in_quiet_hours(_mk(12, 0), ["22:00", "08:00"]))

    def test_no_wrap(self):
        self.assertTrue(_in_quiet_hours(_mk(14, 0), ["13:00", "15:00"]))
        self.assertFalse(_in_quiet_hours(_mk(16, 0), ["13:00", "15:00"]))


def _mk(h, m):
    import time
    # 用本地时间元组构造时间戳，使 time.localtime(ts) 返回指定时分（避开 UTC 偏移）
    return time.mktime((2026, 1, 1, h, m, 0, 0, 0, 0))


class TestGuardrails(unittest.TestCase):
    def setUp(self):
        ProactiveStore._BASE_DIR = tempfile.mkdtemp()
        self.eng = ProactiveEngine()
        self.eng.set_config("dev:1", {"enabled": True})

    def test_frequency_limit(self):
        now = _mk(10, 0)
        for _ in range(3):
            self.eng._store.record_said("dev:1", Candidate("t", "k", 1.0, "x"), "x", now)
        self.assertEqual(self.eng._store.daily_count_today("dev:1", now), 3)
        ok = self.eng._pass_guardrails("dev:1", Candidate("t", "k", 1.0), now)
        self.assertFalse(ok)                          # 达上限封顶

    def test_quiet_blocks(self):
        now = _mk(23, 30)                             # 在 22:00-08:00 静默窗
        self.assertFalse(self.eng._pass_guardrails("dev:1", Candidate("t", "k", 1.0), now))

    def test_dedup_24h(self):
        now = _mk(10, 0)
        self.eng._store.record_said("dev:1", Candidate("silence", "silence", 1.0, "x"), "x", now)
        self.assertTrue(self.eng._store.said_topic_recently("dev:1", "silence", 86400, now))
        self.assertFalse(self.eng._pass_guardrails("dev:1", Candidate("silence", "silence", 1.0), now))
        # 25h 后不去重
        self.assertFalse(self.eng._store.said_topic_recently("dev:1", "silence", 86400, now + 25 * 3600))


class TestTriggers(unittest.TestCase):
    def setUp(self):
        ProactiveStore._BASE_DIR = tempfile.mkdtemp()
        self.eng = ProactiveEngine()
        self.eng.set_config("dev:1", {"enabled": True})

    def test_time_trigger_from_reminder_fired(self):
        self.eng.enqueue_time_trigger("dev:1", {
            "type": "reminder.fired", "rid": "r1", "text": "吃药时间到了"})
        cands = self.eng.evaluate("dev:1")
        rem = [c for c in cands if c.reason == "reminder_related"]
        self.assertEqual(len(rem), 1)
        self.assertIn("吃药", rem[0].text)
        self.assertEqual(rem[0].topic, "reminder:r1")

    def test_silence_trigger_after_dormancy(self):
        now = _mk(12, 0)
        self.eng._store.record_said("dev:1", Candidate("silence", "silence", 1.0, "x"), "x",
                                    now - 4 * 86400)   # 4 天前
        cands = self.eng.evaluate("dev:1", now)
        sil = [c for c in cands if c.reason == "silence_regression"]
        self.assertEqual(len(sil), 1)

    def test_silence_no_trigger_when_recent(self):
        now = _mk(12, 0)
        self.eng._store.record_said("dev:1", Candidate("silence", "silence", 1.0, "x"), "x",
                                    now - 3600)        # 1 小时前
        cands = self.eng.evaluate("dev:1", now)
        self.assertFalse(any(c.reason == "silence_regression" for c in cands))

    def test_random_trigger_gating(self):
        # 随机函数恒为 0 → 必触发；恒为 1 → 不触发
        orig = random.random
        try:
            random.random = lambda: 0.0
            cands = self.eng.evaluate("dev:1")
            self.assertTrue(any(c.reason == "random_surprise" for c in cands))
            random.random = lambda: 1.0
            cands = self.eng.evaluate("dev:1")
            self.assertFalse(any(c.reason == "random_surprise" for c in cands))
        finally:
            random.random = orig

    def test_disabled_device_no_triggers(self):
        self.eng.set_config("dev:2", {"enabled": False})
        self.eng.enqueue_time_trigger("dev:2", {"type": "reminder.fired", "rid": "r2"})
        self.assertEqual(self.eng.evaluate("dev:2"), [])


class TestArbiter(unittest.TestCase):
    def test_picks_highest_score(self):
        cands = [Candidate("random", "random", 0.3),
                 Candidate("reminder_related", "reminder:x", 0.7),
                 Candidate("silence", "silence", 0.5)]
        best = ProactiveEngine.arbitrate(cands)
        self.assertEqual(best.reason, "reminder_related")

    def test_empty(self):
        self.assertIsNone(ProactiveEngine.arbitrate([]))


class TestFireFlow(unittest.TestCase):
    def setUp(self):
        ProactiveStore._BASE_DIR = tempfile.mkdtemp()
        self.eng = ProactiveEngine()
        self.eng.set_config("dev:1", {"enabled": True})
        self.said = []
        self.eng._say = lambda did, text: self._fake_say(did, text)
        self.bus = FakeBus()

    async def _fake_say(self, did, text):
        self.said.append((did, text))
        return True

    def test_fire_updates_state_and_publishes(self):
        now = _mk(10, 0)
        cand = Candidate("silence_regression", "silence", 0.5,
                         "好几天没和你聊天了，最近一切都还顺利吗？")
        with mock.patch("fuguoai_core.utils.event_bus.get_event_bus", return_value=self.bus):
            ok = run(self.eng.fire("dev:1", cand))
        self.assertTrue(ok)
        self.assertEqual(len(self.said), 1)
        # 状态回流
        st = self.eng._store.get_state("dev:1")
        self.assertGreater(st["last_proactive_ts"], 0)  # fire 用真实 time.time() 写入
        self.assertGreaterEqual(self.eng._store.daily_count_today("dev:1"), 1)
        # 事件发布
        self.assertEqual(len(self.bus.published), 1)
        ev = self.bus.published[0]
        self.assertEqual(ev["type"], "proactive.said")
        self.assertEqual(ev["trigger"], "silence_regression")
        self.assertIn("好几天", ev["text"])

    def test_fire_say_failure_no_state(self):
        self.eng._say = lambda did, text: _fail()
        cand = Candidate("manual", "manual", 1.0, "hi")
        with mock.patch("fuguoai_core.utils.event_bus.get_event_bus", return_value=self.bus):
            ok = run(self.eng.fire("dev:1", cand))
        self.assertFalse(ok)
        self.assertEqual(len(self.bus.published), 0)   # 下发失败不发布事件
        self.assertEqual(self.eng._store.daily_count_today("dev:1"), 0)


async def _fail():
    return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
