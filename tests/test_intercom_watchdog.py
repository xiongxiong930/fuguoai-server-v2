"""L3 离线回归：对讲上行静默看门狗 + SetIntercom 下发失败快速收敛（08-01 治标①②）。

背景（08-01 终审诊断）：设备出现应用层"假在线"——后端下行逐帧写入成功
（send_error=0、TCP 活到最后能干净主动关闭），但设备上行永久归零，通话
就这么挂着 41s，手机侧干等、设备侧 far=0。看门狗负责兜底收敛该状态。

看门狗会**主动挂断用户通话**，误杀代价极高，故本测试的重点是三重门的
防误杀边界，而非只验命中：
  命中   ：超免检期 + 设备上行静默 + 手机在发   → 下发 stop + 推 peer_offline + ended(peer_unreachable)
  不动手 A：设备上行健在（正常通话）
  不动手 B：手机也不发（空闲通话，不属看门狗职责）
  不动手 C：仍在免检期（把自举的两次重发机会让完，不与 bootstrap 抢）
  不动手 D：设备↔设备会话（无手机侧参照）
治标②：SetIntercom start 未送达时不谎报 connected，直接 failed(peer_offline)，
并把 peer_offline 推给**存活端**（旧逻辑恒推 rec["a"]，发起端自己掉线时等于没通知）。
"""

import asyncio
import time

import fuguoai_core.utils.connection_registry as cr_mod
import fuguoai_core.utils.intercom_call_store as store_mod
import fuguoai_core.utils.intercom_registry as ir_mod
from fuguoai_core.utils.intercom_call_store import (
    IntercomCallOrchestrator,
    _set_intercom_cmd,
)
from fuguoai_core.utils.metrics import METRICS


def _snap():
    return METRICS.snapshot_counters()


def _delta(before, after, name):
    b = before.get(name, {})
    a = after.get(name, {})
    return {k: a.get(k, 0.0) - b.get(k, 0.0) for k in set(b) | set(a)}


class _FakeIntercom:
    """last_frame_age 由 ages 表驱动；记录 leave 调用供拆会话断言。"""

    def __init__(self, ages: dict, start_result: str = "established"):
        self._ages = ages
        self._start_result = start_result
        self.left = []

    def last_frame_age(self, device_id):
        return self._ages.get(device_id)

    def peer_of(self, device_id):
        return None

    async def start(self, a, b):
        return self._start_result

    async def leave(self, device_id, reason="stop"):
        self.left.append((device_id, reason))


class _FakeReg:
    def __init__(self, send_ok=True, online=True):
        self.calls = []
        self._send_ok = send_ok
        self._online = online

    def is_online(self, device_id):
        return self._online

    async def send_to_device(self, device, cmd):
        self.calls.append((device, cmd))
        return self._send_ok


def _run_watchdog(rec, ages, send_reg, max_rounds=5):
    """跑看门狗，返回 (metrics delta, 下发记录, peer_offline 事件记录, fake_intercom)。

    fake sleep 让巡检零耗时；免检期靠 rec["connected_at"] 取过去时刻来跨越，
    不依赖真实时间流逝。
    """
    fake_intercom = _FakeIntercom(ages)
    events = []

    async def _fake_send_event(device_id, event, peer=None):
        events.append((device_id, event, peer))
        return True

    orig_ir = ir_mod.get_intercom_registry
    orig_cr = cr_mod.get_connection_registry
    orig_ev = ir_mod._send_event
    orig_sleep = asyncio.sleep
    orig_rounds = store_mod.WATCHDOG_MAX_ROUNDS

    async def _fake_sleep(*a, **k):
        return None

    ir_mod.get_intercom_registry = lambda: fake_intercom
    cr_mod.get_connection_registry = lambda: send_reg
    ir_mod._send_event = _fake_send_event
    asyncio.sleep = _fake_sleep
    store_mod.WATCHDOG_MAX_ROUNDS = max_rounds
    orch = IntercomCallOrchestrator()
    try:
        before = _snap()
        asyncio.run(orch._uplink_watchdog(rec))
        after = _snap()
        return (_delta(before, after, "fuguoai_intercom_watchdog_teardown_total"),
                send_reg.calls, events, fake_intercom)
    finally:
        ir_mod.get_intercom_registry = orig_ir
        cr_mod.get_connection_registry = orig_cr
        ir_mod._send_event = orig_ev
        asyncio.sleep = orig_sleep
        store_mod.WATCHDOG_MAX_ROUNDS = orig_rounds


def _rec(status="connected", age_sec=100.0, a="phone-U", b="devY"):
    """构造呼叫单；age_sec = connected 至今的秒数（控制免检期是否跨过）。"""
    return {
        "call_id": "cw", "uid": "u1", "a": a, "b": b,
        "status": status, "reason": None,
        "created_at": time.time() - age_sec,
        "deadline": time.time() + 30,
        "connected_at": time.time() - age_sec,
        "ended_at": None,
    }


# ────────────────────────── 命中：假在线被收敛 ──────────────────────────

def test_watchdog_teardown_when_device_silent_and_phone_active():
    """08-01 真机故障形态：设备上行永久归零、手机持续在发 → 必须收敛。"""
    rec = _rec()
    send_reg = _FakeReg()
    ages = {"devY": None, "phone-U": 0.1}   # 设备从未上行；手机刚发过
    d, calls, events, fi = _run_watchdog(rec, ages, send_reg)

    assert d.get((("device", "devY"),), 0.0) == 1.0, f"应记 teardown 计数, 实得 {d}"
    assert rec["status"] == "ended", f"呼叫单应收敛为 ended, 实得 {rec['status']}"
    assert rec["reason"] == "peer_unreachable", f"reason 应为 peer_unreachable, 实得 {rec['reason']}"
    # 1. 向设备下发 SetIntercom stop 解假在线（手机端不下发 iot 指令）
    assert (("devY", _set_intercom_cmd("phone-U", "stop")) in calls), \
        f"应向设备下发 SetIntercom stop, 实得 {calls}"
    assert all(dev != "phone-U" for dev, _ in calls), f"不应向手机下发 iot 指令, 实得 {calls}"
    # 2. 向手机推 peer_offline
    assert ("phone-U", "peer_offline", "devY") in events, \
        f"应向手机推 peer_offline, 实得 {events}"
    # 3. 低层会话被拆
    assert fi.left, "应拆除低层透传会话"


def test_watchdog_teardown_when_device_stalls_midway():
    """设备发过帧但已静默超阈（>5s）——同样属假在线。"""
    rec = _rec()
    send_reg = _FakeReg()
    ages = {"devY": 7.0, "phone-U": 0.2}
    d, calls, events, _ = _run_watchdog(rec, ages, send_reg)
    assert d.get((("device", "devY"),), 0.0) == 1.0, f"中途塌陷应收敛, 实得 {d}"
    assert ("phone-U", "peer_offline", "devY") in events


def test_watchdog_works_when_device_is_side_a():
    """设备在 a 端、手机在 b 端时同样生效（不假设 a 必为手机）。"""
    rec = _rec(a="devZ", b="phone-V")
    send_reg = _FakeReg()
    ages = {"devZ": None, "phone-V": 0.1}
    d, calls, events, _ = _run_watchdog(rec, ages, send_reg)
    assert d.get((("device", "devZ"),), 0.0) == 1.0, f"应收敛, 实得 {d}"
    assert ("phone-V", "peer_offline", "devZ") in events, f"实得 {events}"


# ────────────────────────── 防误杀：四道边界 ──────────────────────────

def test_watchdog_no_teardown_when_device_uplink_alive():
    """正常通话：设备上行健在 → 绝不能挂断。"""
    rec = _rec()
    send_reg = _FakeReg()
    ages = {"devY": 0.1, "phone-U": 0.1}
    d, calls, events, _ = _run_watchdog(rec, ages, send_reg)
    assert not d or all(v == 0.0 for v in d.values()), f"不应 teardown, 实得 {d}"
    assert rec["status"] == "connected", "通话应保持"
    assert calls == [], f"不应下发任何指令, 实得 {calls}"
    assert events == [], f"不应推任何事件, 实得 {events}"


def test_watchdog_no_teardown_when_phone_also_silent():
    """空闲通话(新建、仍在 stale 宽限内)：双方都没发帧 → 不属看门狗职责，不动手。

    防误杀护栏：刚接通(age_sec=10，远小于 stale_max=60)且两端都还没说话的空闲通话，
    即使设备此时一帧没发，也不能贸然挂断——设备可能正在启动音频通道、或用户还没开口。
    stale 兜底只在设备静默**超过 stale_max(60s)** 时才动手（见 test_watchdog_stale_*）。
    """
    rec = _rec(age_sec=10.0)
    send_reg = _FakeReg()
    ages = {"devY": None, "phone-U": None}
    d, calls, events, _ = _run_watchdog(rec, ages, send_reg)
    assert not d or all(v == 0.0 for v in d.values()), f"新建空闲通话不应 teardown, 实得 {d}"
    assert rec["status"] == "connected"
    assert calls == [] and events == []


def test_watchdog_stale_reap_when_device_never_sent():
    """stale 兜底①：设备从未发过上行帧、且通话已 idle 超 stale_max(60s) → 判死拆。

    对应"重连残 call"真场景：设备重连后从不发 uplink(_last_frame_ts 无记录→dev_age=None)、
    手机也沉默，正常门3(手机在发)永远不满足 → 旧实现累积 25min 孤本。stale_dead 以
    call 年龄代替缺失的 dev_age，超 stale_max 直接强拆，绕过门3。
    """
    rec = _rec(age_sec=100.0)
    send_reg = _FakeReg()
    ages = {"devY": None, "phone-U": None}
    d, calls, events, _ = _run_watchdog(rec, ages, send_reg)
    assert d.get((("device", "devY"),), 0.0) == 1.0, f"stale残call应强拆, 实得 {d}"
    assert rec["status"] == "ended", f"应收敛为 ended, 实得 {rec['status']}"
    assert rec["reason"] == "peer_unreachable", f"reason 应为 peer_unreachable, 实得 {rec['reason']}"
    assert ("phone-U", "peer_offline", "devY") in events, f"应推 peer_offline, 实得 {events}"


def test_watchdog_stale_reap_when_device_stalled_long():
    """stale 兜底②：设备曾发帧(has_uplink_flowed=True)但重连后静默、dev_age 累积超
    stale_max(60s) → 判死拆。

    对应"曾流过→重连静默"的真 stale 残call（旧实现 has_uplink_flowed 一旦置 True 永不清零、
    把 stale_dead 卡死成死代码的根因）。stale_dead 改为以"有效静默时长"判定(不依赖
    has_flowed)，dev_age>stale_max 即强拆，绕过门3(手机沉默)。本测试显式置
    has_uplink_flowed=True 以证明该旧 bug 已根治。
    """
    rec = _rec(age_sec=100.0)
    rec["has_uplink_flowed"] = True
    send_reg = _FakeReg()
    ages = {"devY": 100.0, "phone-U": None}
    d, calls, events, _ = _run_watchdog(rec, ages, send_reg)
    assert d.get((("device", "devY"),), 0.0) == 1.0, f"stale残call应强拆, 实得 {d}"
    assert rec["status"] == "ended", f"应收敛为 ended, 实得 {rec['status']}"
    assert ("phone-U", "peer_offline", "devY") in events, f"应推 peer_offline, 实得 {events}"


def test_watchdog_no_teardown_during_min_age_grace():
    """免检期内（connected 仅 1s）：把自举的两次重发机会让完，不抢跑。"""
    rec = _rec(age_sec=1.0)
    send_reg = _FakeReg()
    ages = {"devY": None, "phone-U": 0.1}   # 命中条件已满足，但仍在免检期
    d, calls, events, _ = _run_watchdog(rec, ages, send_reg)
    assert not d or all(v == 0.0 for v in d.values()), f"免检期内不应 teardown, 实得 {d}"
    assert rec["status"] == "connected"
    assert calls == []


def test_watchdog_ignores_device_to_device_session():
    rec = _rec(a="devA", b="devB")
    send_reg = _FakeReg()
    ages = {"devA": None, "devB": None}
    d, calls, events, _ = _run_watchdog(rec, ages, send_reg)
    assert not d or all(v == 0.0 for v in d.values()), f"设备↔设备不应介入, 实得 {d}"
    assert calls == []


def test_watchdog_exits_when_call_already_ended():
    """呼叫已终态 → 守望立即退出，不重复收敛（与 worker/挂断并发安全）。"""
    rec = _rec(status="ended")
    send_reg = _FakeReg()
    ages = {"devY": None, "phone-U": 0.1}
    d, calls, events, _ = _run_watchdog(rec, ages, send_reg)
    assert not d or all(v == 0.0 for v in d.values()), f"终态不应再 teardown, 实得 {d}"
    assert calls == []


# ────────────────── 治标②：SetIntercom 下发失败快速收敛 ──────────────────

def _run_try_connect(rec, send_ok):
    fake_intercom = _FakeIntercom({})
    events = []

    async def _fake_send_event(device_id, event, peer=None):
        events.append((device_id, event, peer))
        return True

    send_reg = _FakeReg(send_ok=send_ok, online=True)
    orig_ir = ir_mod.get_intercom_registry
    orig_cr = cr_mod.get_connection_registry
    orig_ev = ir_mod._send_event
    ir_mod.get_intercom_registry = lambda: fake_intercom
    cr_mod.get_connection_registry = lambda: send_reg
    ir_mod._send_event = _fake_send_event
    orch = IntercomCallOrchestrator()
    # 隔离后台守望任务，避免 asyncio.run 收尾时留未完成 task
    orch._schedule_bootstrap = lambda r: None
    orch._schedule_watchdog = lambda r: None
    try:
        before = _snap()
        asyncio.run(orch._try_connect(rec))
        after = _snap()
        return (_delta(before, after, "fuguoai_intercom_send_start_fail_total"),
                send_reg.calls, events, fake_intercom)
    finally:
        ir_mod.get_intercom_registry = orig_ir
        cr_mod.get_connection_registry = orig_cr
        ir_mod._send_event = orig_ev


def test_try_connect_fails_fast_when_setintercom_not_delivered():
    """设备离线/发送异常 → 不谎报 connected，立即 failed(peer_offline)。"""
    rec = _rec(status="calling")
    d, calls, events, fi = _run_try_connect(rec, send_ok=False)
    assert rec["status"] == "failed", f"应判 failed, 实得 {rec['status']}"
    assert rec["reason"] == "peer_offline", f"reason 应为 peer_offline, 实得 {rec['reason']}"
    assert d.get((("device", "devY"),), 0.0) == 1.0, f"应记下发失败计数, 实得 {d}"
    # peer_offline 必须推给存活端（手机），而不是掉线的设备
    assert ("phone-U", "peer_offline", "devY") in events, \
        f"peer_offline 应推给存活的手机端, 实得 {events}"
    assert fi.left, "已建的低层会话应被回滚"


def test_try_connect_normal_path_still_connects():
    """回归保护：下发成功时行为不变，仍进 connected。"""
    rec = _rec(status="calling")
    d, calls, events, _ = _run_try_connect(rec, send_ok=True)
    assert rec["status"] == "connected", f"正常路径应 connected, 实得 {rec['status']}"
    assert not d or all(v == 0.0 for v in d.values()), f"不应记失败计数, 实得 {d}"
    assert (("devY", _set_intercom_cmd("phone-U", "start")) in calls), \
        f"应向设备下发 start, 实得 {calls}"


if __name__ == "__main__":
    test_watchdog_teardown_when_device_silent_and_phone_active()
    test_watchdog_teardown_when_device_stalls_midway()
    test_watchdog_works_when_device_is_side_a()
    test_watchdog_no_teardown_when_device_uplink_alive()
    test_watchdog_no_teardown_when_phone_also_silent()
    test_watchdog_stale_reap_when_device_never_sent()
    test_watchdog_stale_reap_when_device_stalled_long()
    test_watchdog_no_teardown_during_min_age_grace()
    test_watchdog_ignores_device_to_device_session()
    test_watchdog_exits_when_call_already_ended()
    test_try_connect_fails_fast_when_setintercom_not_delivered()
    test_try_connect_normal_path_still_connects()
    print("ALL_INTERCOM_WATCHDOG_TESTS_PASS")
