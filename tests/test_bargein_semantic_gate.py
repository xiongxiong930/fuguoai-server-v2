"""barge-in 语义门控 R1/R2 离线单测（多票延迟裁决 + 保守降级）。

用 unittest.mock 替换声纹 provider，验证：
  1. 门控关闭 → ACCEPT（零行为变更）
  2. R1 灰度（reject 开关关）→ ACCEPT（安静）
  3. R2 启用 + 两次"未知说话人" → 第 3 次 REJECT（多票防单帧噪声误拒）
  4. R2 启用 + 识别到已知成员(name) → 持续 ACCEPT
  5. 声纹 provider=None（未启用）→ ACCEPT 不 reject（保守降级）
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch
import fuguoai_core.utils.bargein_semantic_gate as G

SR = G._SR
BYTES_PER_SEC = G._BYTES_PER_SEC


class _Conn:
    config = None
    device_id = "testdev"

    class logger:
        @staticmethod
        def bind(**k):
            class _L:
                def info(self, *a, **k):
                    pass

                def warning(self, *a, **k):
                    pass
            return _L()


class _FakeVP:
    def __init__(self, ret):
        self.ret = ret

    async def identify_speaker(self, wav, session_id):
        return self.ret


def _chunk(sec: float = 2.0) -> bytes:
    # 静音 int16 PCM（内容无关，provider 被 mock）
    return b"\x00" * int(BYTES_PER_SEC * sec)


async def test_gate_off():
    c = _Conn()
    c.config = {}
    st = await G.semantic_bargein_decide(c, _chunk(), 1000.0, 200.0)
    assert st == G.BargeState.ACCEPT, st
    print("PASS[1] gate_off -> ACCEPT")


async def test_reject_off():
    c = _Conn()
    c.config = {"bargein_semantic_gate_enabled": True}
    st = await G.semantic_bargein_decide(c, _chunk(), 1000.0, 200.0)
    assert st == G.BargeState.ACCEPT, st
    print("PASS[2] reject_off -> ACCEPT (R1 灰度安静)")


async def test_reject_on_unknown_two_votes():
    c = _Conn()
    c.config = {"bargein_semantic_gate_enabled": True,
                "bargein_reject_unknown_speaker": True}
    with patch.object(G, "_REID_INTERVAL", 0):
        with patch("fuguoai_core.utils.voiceprint_provider.get_voiceprint_provider",
                   return_value=_FakeVP("未知说话人")):
            st1 = await G.semantic_bargein_decide(c, _chunk(), 1000.0, 200.0)
            gate = getattr(c, G._GATE_ATTR)
            await gate["task"]
            st2 = await G.semantic_bargein_decide(c, _chunk(), 1000.0, 200.0)
            await gate["task"]
            st3 = await G.semantic_bargein_decide(c, _chunk(), 1000.0, 200.0)
    assert st1 == G.BargeState.ACCEPT and st2 == G.BargeState.ACCEPT, (st1, st2)
    assert st3 == G.BargeState.REJECT, st3
    print("PASS[3] reject_on 两次未知 -> REJECT（多票防误拒）")


async def test_reject_on_known_name():
    c = _Conn()
    c.config = {"bargein_semantic_gate_enabled": True,
                "bargein_reject_unknown_speaker": True}
    with patch.object(G, "_REID_INTERVAL", 0):
        with patch("fuguoai_core.utils.voiceprint_provider.get_voiceprint_provider",
                   return_value=_FakeVP("爸爸")):
            st1 = await G.semantic_bargein_decide(c, _chunk(), 1000.0, 200.0)
            gate = getattr(c, G._GATE_ATTR)
            await gate["task"]
            st2 = await G.semantic_bargein_decide(c, _chunk(), 1000.0, 200.0)
    assert st1 == G.BargeState.ACCEPT and st2 == G.BargeState.ACCEPT, (st1, st2)
    print("PASS[4] reject_on 已知成员(name) -> 持续 ACCEPT")


async def test_provider_none():
    c = _Conn()
    c.config = {"bargein_semantic_gate_enabled": True,
                "bargein_reject_unknown_speaker": True}
    with patch.object(G, "_REID_INTERVAL", 0):
        with patch("fuguoai_core.utils.voiceprint_provider.get_voiceprint_provider",
                   return_value=None):
            st1 = await G.semantic_bargein_decide(c, _chunk(), 1000.0, 200.0)
            gate = getattr(c, G._GATE_ATTR)
            await gate["task"]
            st2 = await G.semantic_bargein_decide(c, _chunk(), 1000.0, 200.0)
    assert st1 == G.BargeState.ACCEPT and st2 == G.BargeState.ACCEPT, (st1, st2)
    print("PASS[5] provider=None(声纹未启用) -> ACCEPT 不 reject")


async def main():
    await test_gate_off()
    await test_reject_off()
    await test_reject_on_unknown_two_votes()
    await test_reject_on_known_name()
    await test_provider_none()
    print("ALL BARGEIN_GATE TESTS PASSED")


if __name__ == "__main__":
    # 预导入（loop 外）：voiceprint_provider 顶层 setup_logging() 会调 asyncio.run，
    # 若在 asyncio.run(main) 内才被 patch 触发 import 会冲突。
    import fuguoai_core.utils.voiceprint_provider  # noqa: F401
    asyncio.run(main())
