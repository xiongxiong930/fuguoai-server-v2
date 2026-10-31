"""L3 离线回归：intercom relay 可观测性(METRICS) + 首连自举重发 SetIntercom。

覆盖：
- relay() 成功转发记 downlink/uplink 计数 + 记 last_frame_ts；
- relay() 各丢弃分支记 drop_total{reason}；
- 首连自举 _bootstrap_monitor 决策：
    A. 设备静默 + 手机在发 → 重发 SetIntercom start（覆盖段2 首连死）
    B. 设备已上行 → 不重发
    C. 设备↔设备（无 phone） → 不重发
    D. 设备静默 + 手机没发 → 不重发
- intercom_registry.last_frame_age 三态（None / 小 / 大）。
"""

import asyncio
import time

import fuguoai_core.utils.intercom_call_store as store_mod
import fuguoai_core.utils.intercom_registry as ir_mod
from fuguoai_core.utils.intercom_call_store import (
    IntercomCallOrchestrator,
    _set_intercom_cmd,
)
from fuguoai_core.utils.intercom_registry import IntercomRegistry
from fuguoai_core.utils.metrics import METRICS


def _snap_counters():
    return METRICS.snapshot_counters()


def _delta(before, after, name):
    b = before.get(name, {})
    a = after.get(name, {})
    d = {}
    for k in set(b) | set(a):
        d[k] = a.get(k, 0.0) - b.get(k, 0.0)
    return d


class _FakeConn:
    def __init__(self):
        self.sent = []

    async def safe_send_bytes(self, data):
        self.sent.append(data)
        return True


class _FakeIntercom:
    """用于 bootstrap 测试：按 device 返回 last_frame_age，按 device 返回 peer。

    ages 值可为标量（每轮恒定）或 list（按巡检轮次逐个弹出，末值保持），
    后者用于复现「设备先发几帧后塌陷」的真实故障时序。
    """

    def __init__(self, ages: dict, peers: dict):
        self._ages = ages
        self._peers = peers
        self._cursor = {}

    def last_frame_age(self, device_id):
        v = self._ages.get(device_id)
        if isinstance(v, list):
            i = self._cursor.get(device_id, 0)
            self._cursor[device_id] = min(i + 1, len(v) - 1)
            return v[i]
        return v

    def peer_of(self, device_id):
        return self._peers.get(device_id)


class _FakeReg:
    def __init__(self):
        self.calls = []

    async def send_to_device(self, device, cmd):
        self.calls.append((device, cmd))
        return True


def test_relay_downlink_and_last_frame():
    reg = IntercomRegistry()
    a, b = "devA", "devB"
    sid = "s1"
    with reg._lock:
        reg._sessions[sid] = {"a": a, "b": b, "state": "established",
                              "created_at": time.time(), "grace_deadline": 0.0,
                              "grace_device": None}
        reg._by_device[a] = sid
        reg._by_device[b] = sid
    fake = _FakeConn()
    orig = ir_mod._get_conn
    ir_mod._get_conn = lambda did: fake
    try:
        frame = b"\x00\x00\x00\x01" + b"\x00" * 20  # 设备↔设备：同类型原样透传
        before = _snap_counters()
        ok = asyncio.run(reg.relay(a, frame))
        after = _snap_counters()
        assert ok is True, "relay 应成功"
        # downlink 计数 +1（target=devB 非 phone）
        d = _delta(before, after, "fuguoai_intercom_relay_downlink_total")
        assert d.get((("from", a), ("to", b))) == 1.0, f"downlink 应+1, 实得 {d}"
        # last_frame_ts 已记
        with reg._lock:
            assert a in reg._last_frame_ts, "应记录来源端发帧时刻"
    finally:
        ir_mod._get_conn = orig


def test_relay_drop_not_registered():
    reg = IntercomRegistry()
    fake = _FakeConn()
    orig = ir_mod._get_conn
    ir_mod._get_conn = lambda did: fake
    try:
        before = _snap_counters()
        ok = asyncio.run(reg.relay("ghost", b"\x00" * 8))
        after = _snap_counters()
        assert ok is False, "未注册应丢弃"
        d = _delta(before, after, "fuguoai_intercom_relay_drop_total")
        assert d.get((("reason", "not_registered"),)) == 1.0, f"应记 not_registered, 实得 {d}"
    finally:
        ir_mod._get_conn = orig


def test_last_frame_age_states():
    reg = IntercomRegistry()
    with reg._lock:
        reg._last_frame_ts["x"] = time.time()        # 刚发
        reg._last_frame_ts["y"] = time.time() - 999  # 很久以前
    assert reg.last_frame_age("x") is not None and reg.last_frame_age("x") < 1.0
    assert reg.last_frame_age("y") > 900
    assert reg.last_frame_age("never") is None


def _run_bootstrap(ages, peers, rec, send_reg,
                   metric="fuguoai_intercom_bootstrap_resend_total"):
    fake_intercom = _FakeIntercom(ages, peers)
    orig_ir = ir_mod.get_intercom_registry
    orig_cr = store_mod.__dict__.get("get_connection_registry")  # noqa
    # 直接 patch 被 import 的模块函数
    ir_mod.get_intercom_registry = lambda: fake_intercom
    # connection_registry.get_connection_registry 在 monitor 内 import
    import fuguoai_core.utils.connection_registry as cr_mod
    orig_cr_fn = cr_mod.get_connection_registry
    cr_mod.get_connection_registry = lambda: send_reg
    # 让 asyncio.sleep 立即返回，避免 2.5s×2 真实等待
    orig_sleep = asyncio.sleep
    async def _fake_sleep(*a, **k):
        return None
    asyncio.sleep = _fake_sleep
    orch = IntercomCallOrchestrator()
    try:
        before = _snap_counters()
        asyncio.run(orch._bootstrap_monitor(rec))
        after = _snap_counters()
        return _delta(before, after, metric)
    finally:
        ir_mod.get_intercom_registry = orig_ir
        cr_mod.get_connection_registry = orig_cr_fn
        asyncio.sleep = orig_sleep


def test_bootstrap_resend_when_device_silent_and_phone_talking():
    send_reg = _FakeReg()
    rec = {"call_id": "c1", "a": "phone-U", "b": "devY", "status": "connected"}
    ages = {"devY": None, "phone-U": 0.1}
    peers = {"devY": "phone-U"}
    d = _run_bootstrap(ages, peers, rec, send_reg)
    # 持续静默 + 手机持续在发：重发次数被 BOOTSTRAP_MAX_RESEND 硬性封顶为 2（8 轮巡检不刷屏）
    assert d.get((("device", "devY"),)) == 2.0, f"应自举重发2次(上限), 实得 {d}"
    assert len(send_reg.calls) == 2, f"应下发2次 SetIntercom, 实得 {send_reg.calls}"
    dev, cmd = send_reg.calls[0]
    assert dev == "devY"
    assert cmd == _set_intercom_cmd("phone-U", "start")


def test_bootstrap_resend_when_device_stalls_after_a_few_frames():
    """回归 07-31 真机故障：设备 connected 后发 ~7 帧(约 140ms)即塌陷。

    首轮体检时 dev_age=1.0s 尚未越过静默阈值(1.2s)，看似「设备健在」——旧实现
    在此 return 永久放弃，导致自举从未触发（metrics resend 恒空）。修复后应
    continue 继续守望，在后续轮次 age 增长越阈时重发。
    """
    send_reg = _FakeReg()
    rec = {"call_id": "c5", "a": "phone-U", "b": "devY", "status": "connected"}
    # 设备：首轮 1.0s(未越阈) → 之后持续增长越阈；手机全程在说话
    ages = {"devY": [1.0, 2.5, 4.0, 5.5], "phone-U": 0.1}
    peers = {"devY": "phone-U"}
    d = _run_bootstrap(ages, peers, rec, send_reg)
    assert len(send_reg.calls) >= 1, f"设备中途塌陷应被守望到并重发, 实得 {send_reg.calls}"
    assert d.get((("device", "devY"),), 0.0) >= 1.0, f"应记 resend 计数, 实得 {d}"


def test_bootstrap_records_uplink_stall_metric():
    """塌陷事实必须独立于重发结果落 metrics，便于定责固件 vs 后端。"""
    send_reg = _FakeReg()
    rec = {"call_id": "c6", "a": "phone-U", "b": "devY", "status": "connected"}
    ages = {"devY": None, "phone-U": 0.1}
    peers = {"devY": "phone-U"}
    d = _run_bootstrap(ages, peers, rec, send_reg,
                       metric="fuguoai_intercom_uplink_stall_total")
    assert d.get((("device", "devY"),), 0.0) >= 1.0, f"应记设备上行塌陷计数, 实得 {d}"


def test_bootstrap_no_resend_when_device_talking():
    send_reg = _FakeReg()
    rec = {"call_id": "c2", "a": "phone-U", "b": "devY", "status": "connected"}
    ages = {"devY": 0.1, "phone-U": 0.1}
    peers = {"devY": "phone-U"}
    _run_bootstrap(ages, peers, rec, send_reg)
    assert len(send_reg.calls) == 0, f"设备已上行不应重发, 实得 {send_reg.calls}"


def test_bootstrap_no_resend_device_to_device():
    send_reg = _FakeReg()
    rec = {"call_id": "c3", "a": "devA", "b": "devB", "status": "connected"}
    ages = {"devA": None, "devB": None}
    peers = {}
    _run_bootstrap(ages, peers, rec, send_reg)
    assert len(send_reg.calls) == 0, f"设备↔设备不应自举, 实得 {send_reg.calls}"


def test_bootstrap_no_resend_when_phone_silent():
    send_reg = _FakeReg()
    rec = {"call_id": "c4", "a": "phone-U", "b": "devY", "status": "connected"}
    ages = {"devY": None, "phone-U": None}
    peers = {"devY": "phone-U"}
    _run_bootstrap(ages, peers, rec, send_reg)
    assert len(send_reg.calls) == 0, f"手机没发不应重发, 实得 {send_reg.calls}"


def test_relay_grace_buffer_flush_on_reconnect():
    """回归 08-07：对端短暂断连(grace, 突发重连风暴)期间, 仍在线一端的上行帧应被
    缓冲(而非丢弃); 对端 grace 内重连恢复(established)后由 _flush_grace 补齐。

    用设备↔设备会话(绕过 phone 转码)验证：
    - grace 期 relay 返回 False, 但记 buffered_total(N), 不记 session_not_established drop
    - on_reconnect 后缓冲帧被重放至对端(conn_b.sent==N)
    """
    async def _scenario():
        reg = IntercomRegistry()
        a, b = "devA", "devB"
        sid = "s1"
        with reg._lock:
            reg._sessions[sid] = {"a": a, "b": b, "state": "established",
                                  "created_at": time.time(), "grace_deadline": 0.0,
                                  "grace_device": None}
            reg._by_device[a] = sid
            reg._by_device[b] = sid
        conn_a = _FakeConn()
        conn_b = _FakeConn()

        def _fake_get_conn(did):
            return conn_a if did == a else conn_b

        orig = ir_mod._get_conn
        ir_mod._get_conn = _fake_get_conn
        try:
            frame = b"\x00\x00\x00\x01" + b"\x00" * 20
            # 1) 对端 b 断连 → grace
            reg.on_disconnect(b)
            # 2) a 在 grace 期持续上行 N 帧 → 应缓冲(非丢弃)
            before = _snap_counters()
            n = 10
            for _ in range(n):
                ok = await reg.relay(a, frame)
                assert ok is False, "grace 期应缓冲返回 False"
            after = _snap_counters()
            dbuf = _delta(before, after, "fuguoai_intercom_relay_buffered_total")
            assert dbuf.get((("from", a),)) == float(n), f"应缓冲 {n} 帧, 实得 {dbuf}"
            ddrop = _delta(before, after, "fuguoai_intercom_relay_drop_total")
            assert ddrop.get((("reason", "session_not_established"),), 0.0) == 0.0, \
                f"grace 缓冲不应记 session_not_established drop, 实得 {ddrop}"
            # 3) b 重连 → 恢复 established + 调度 _flush_grace
            reg.on_reconnect(b)
            for _ in range(10):  # 让 flush 任务跑完
                await asyncio.sleep(0)
            # 4) 缓冲帧应补齐至 b
            assert len(conn_b.sent) == n, f"b 应收到补齐的 {n} 帧, 实得 {len(conn_b.sent)}"
            # a 不应收到自己发的(非 loopback)
            assert len(conn_a.sent) == 0, f"a 不应收到回环帧, 实得 {len(conn_a.sent)}"
        finally:
            ir_mod._get_conn = orig

    asyncio.run(_scenario())


if __name__ == "__main__":
    test_relay_downlink_and_last_frame()
    test_relay_drop_not_registered()
    test_last_frame_age_states()
    test_bootstrap_resend_when_device_silent_and_phone_talking()
    test_bootstrap_resend_when_device_stalls_after_a_few_frames()
    test_bootstrap_records_uplink_stall_metric()
    test_bootstrap_no_resend_when_device_talking()
    test_bootstrap_no_resend_device_to_device()
    test_bootstrap_no_resend_when_phone_silent()
    test_relay_grace_buffer_flush_on_reconnect()
    print("ALL_INTERCOM_BOOTSTRAP_METRICS_TESTS_PASS")
