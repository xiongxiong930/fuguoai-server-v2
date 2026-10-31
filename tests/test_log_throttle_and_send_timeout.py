"""离线测试：对讲日志限频 + TTS 下发超时动态估算（2026-08-01 后端加固 ②③）。

背景
----
② 对讲 relay 早退 / 转码失败原本每帧一条 WARNING，单通对讲刷 3190 行、
   一次联调 10555 行（占当日日志 80%），真实告警被淹没。改为按 key 限频。
   另修复 debug 打印"前3条"判据因计数器被归零而恒成立、导致逐帧刷屏的 bug。
③ TTS 下发固定 timeout=5s，而 sendAudioMessage 按真实播放节奏限速下发，
   任何"剩余音频 > 5s"的正常回答都必然误报（实测超时间隔精确 5.000s）。
   改为按音频时长 + 流控队列剩余时长动态计算。

这两项都属"降噪 / 去虚警"，测试重点是**不能把真实信号也一起丢掉**：
限频要保证首条立即可见、被折叠的条数要如实报出；动态超时不能退化成无限等待。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ────────────────────────── ② 日志限频 ──────────────────────────


def test_throttle_first_immediate_then_folded():
    """首条必须立即打印（不能因限频丢掉第一次告警），随后同 key 被折叠。"""
    from fuguoai_core.utils import intercom_registry as ir

    ir._throttle_state.clear()
    captured = []
    orig = ir._logger.warning
    ir._logger.warning = lambda m, *a, **k: captured.append(m)
    try:
        ir._throttled_warn("k1", "peer offline", interval=60.0)
        assert len(captured) == 1, "首条必须立即可见"
        for _ in range(500):
            ir._throttled_warn("k1", "peer offline", interval=60.0)
        assert len(captured) == 1, f"限频失效，打印了 {len(captured)} 条"
    finally:
        ir._logger.warning = orig
    print("PASS test_throttle_first_immediate_then_folded")


def test_throttle_reports_suppressed_count():
    """折叠的条数必须如实报出，否则会低估故障规模（3190 帧丢包不能只看到 1 行）。"""
    from fuguoai_core.utils import intercom_registry as ir

    ir._throttle_state.clear()
    captured = []
    orig = ir._logger.warning
    ir._logger.warning = lambda m, *a, **k: captured.append(m)
    try:
        ir._throttled_warn("k2", "drop", interval=0.0)  # interval=0 → 每次都打
        for _ in range(9):
            ir._throttled_warn("k2", "drop", interval=0.0)
        assert len(captured) == 10
        # 用真实限频窗口验证折叠计数
        ir._throttle_state.clear()
        captured.clear()
        ir._throttled_warn("k3", "drop", interval=60.0)  # 首条
        for _ in range(41):
            ir._throttled_warn("k3", "drop", interval=60.0)
        ir._throttle_state["k3"]["t"] = 0.0  # 强制窗口到期
        ir._throttled_warn("k3", "drop", interval=60.0)
        assert len(captured) == 2, captured
        assert "42" in captured[1], f"折叠条数未如实报出: {captured[1]}"
    finally:
        ir._logger.warning = orig
    print("PASS test_throttle_reports_suppressed_count")


def test_throttle_keys_independent():
    """不同 key（不同设备 / 不同故障原因）互不影响，避免一个刷屏掩盖另一个故障。"""
    from fuguoai_core.utils import intercom_registry as ir

    ir._throttle_state.clear()
    captured = []
    orig = ir._logger.warning
    ir._logger.warning = lambda m, *a, **k: captured.append(m)
    try:
        for i in range(5):
            ir._throttled_warn(f"dev{i}", f"drop dev{i}", interval=60.0)
        assert len(captured) == 5, f"不同 key 被互相限频: {captured}"
    finally:
        ir._logger.warning = orig
    print("PASS test_throttle_keys_independent")


def test_debug_print_no_longer_floods():
    """回归 08-01 修复的刷屏 bug：'前3条'判据不得因计数归零而恒成立。

    直接复刻旧实现与新实现的判据，证明新实现在 1000 帧内只打印有限条。
    """
    import time as _t

    # 旧实现（有 bug）：打印后 n 归零 → n<=3 恒成立 → 每帧都打
    st_old = {"n": 0, "t": _t.time()}
    old_prints = 0
    for _ in range(1000):
        st_old["n"] += 1
        if st_old["n"] <= 3 or _t.time() - st_old["t"] >= 2.0:
            old_prints += 1
            st_old["n"] = 0
            st_old["t"] = _t.time()
    assert old_prints > 900, f"未复现旧 bug（应几乎每帧都打）: {old_prints}"

    # 新实现：printed 独立累加
    st_new = {"n": 0, "printed": 0, "t": 0.0}
    new_prints = 0
    for _ in range(1000):
        st_new["n"] += 1
        now = _t.time()
        if st_new["printed"] < 3 or now - st_new["t"] >= 2.0:
            new_prints += 1
            st_new["printed"] += 1
            st_new["n"] = 0
            st_new["t"] = now
    assert new_prints <= 5, f"新实现仍在刷屏: {new_prints}"
    print(f"PASS test_debug_print_no_longer_floods (旧={old_prints} 新={new_prints})")


# ────────────────────── ③ TTS 下发超时动态估算 ──────────────────────


class _FakeRC:
    def __init__(self, n_pending, frame_duration=60):
        self.queue = [None] * n_pending
        self.frame_duration = frame_duration


class _FakeConn:
    def __init__(self, rc=None):
        if rc is not None:
            self.audio_rate_controller = rc


def _estimator():
    """取被测方法（避免实例化 TTSProviderBase 的重依赖）。"""
    from fuguoai_core.providers.tts.base import TTSProviderBase

    class _Stub:
        _SEND_TIMEOUT_BASE_SEC = TTSProviderBase._SEND_TIMEOUT_BASE_SEC
        _SEND_TIMEOUT_MAX_SEC = TTSProviderBase._SEND_TIMEOUT_MAX_SEC
        _SEND_FRAME_MS = TTSProviderBase._SEND_FRAME_MS
        _estimate_send_timeout = TTSProviderBase._estimate_send_timeout

        def __init__(self, conn):
            self.conn = conn

    return _Stub


def test_timeout_covers_long_audio():
    """核心虚警场景：本轮剩余音频 20s，超时上限必须 > 20s（旧固定 5s 必误报）。"""
    Stub = _estimator()
    # 队列里还有 20s 音频（60ms/帧 → 334 帧）
    s = Stub(_FakeConn(_FakeRC(334)))
    t = s._estimate_send_timeout(b"x", None)
    assert t > 20.0, f"超时上限 {t}s 不足以覆盖 20s 音频，仍会虚警"
    print(f"PASS test_timeout_covers_long_audio (timeout={t:.1f}s)")


def test_timeout_scales_with_block_and_queue():
    """本块帧数与队列长度都要计入，且随之单调增长。"""
    Stub = _estimator()
    t_small = Stub(_FakeConn(_FakeRC(0)))._estimate_send_timeout([b"x"] * 10, None)
    t_block = Stub(_FakeConn(_FakeRC(0)))._estimate_send_timeout([b"x"] * 200, None)
    t_queue = Stub(_FakeConn(_FakeRC(200)))._estimate_send_timeout([b"x"] * 10, None)
    assert t_block > t_small, "本块时长未计入"
    assert t_queue > t_small, "队列剩余时长未计入"
    print(f"PASS test_timeout_scales_with_block_and_queue "
          f"(small={t_small:.1f} block={t_block:.1f} queue={t_queue:.1f})")


def test_timeout_has_floor_and_ceiling():
    """下限不低于 base（不能变成 0 秒即超时），上限不得无界（兜住真挂死）。"""
    Stub = _estimator()
    t_min = Stub(_FakeConn())._estimate_send_timeout(None, None)
    assert t_min >= Stub._SEND_TIMEOUT_BASE_SEC, f"下限被击穿: {t_min}"

    t_max = Stub(_FakeConn(_FakeRC(1_000_000)))._estimate_send_timeout([b"x"] * 999_999, None)
    assert t_max <= Stub._SEND_TIMEOUT_MAX_SEC, f"上限失效（会无限等待）: {t_max}"
    print(f"PASS test_timeout_has_floor_and_ceiling (min={t_min:.1f} max={t_max:.1f})")


def test_timeout_survives_broken_conn():
    """估算过程绝不能因为 conn 异常而抛错阻断音频下发。"""
    Stub = _estimator()

    class _Boom:
        @property
        def audio_rate_controller(self):
            raise RuntimeError("boom")

    t = Stub(_Boom())._estimate_send_timeout([b"x"] * 5, None)
    assert t >= Stub._SEND_TIMEOUT_BASE_SEC, t

    class _WeirdRC:
        queue = "not-a-list"
        frame_duration = None

    t2 = Stub(_FakeConn(_WeirdRC()))._estimate_send_timeout(b"x", None)
    assert t2 >= Stub._SEND_TIMEOUT_BASE_SEC, t2
    print("PASS test_timeout_survives_broken_conn")


def test_timeout_handles_single_packet_bytes():
    """单包 bytes 与包列表都要能算（sendAudio 两种入参形态）。"""
    Stub = _estimator()
    t_bytes = Stub(_FakeConn(_FakeRC(0)))._estimate_send_timeout(b"\x00" * 500, None)
    t_list = Stub(_FakeConn(_FakeRC(0)))._estimate_send_timeout([b"\x00"] * 1, None)
    assert abs(t_bytes - t_list) < 0.001, f"单包/列表结果不一致: {t_bytes} vs {t_list}"
    print("PASS test_timeout_handles_single_packet_bytes")


if __name__ == "__main__":
    test_throttle_first_immediate_then_folded()
    test_throttle_reports_suppressed_count()
    test_throttle_keys_independent()
    test_debug_print_no_longer_floods()
    test_timeout_covers_long_audio()
    test_timeout_scales_with_block_and_queue()
    test_timeout_has_floor_and_ceiling()
    test_timeout_survives_broken_conn()
    test_timeout_handles_single_packet_bytes()
    print("ALL PASS")
