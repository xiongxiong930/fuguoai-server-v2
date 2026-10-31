"""播放期 barge-in 绝对残差门限自适应（#259）离线单测。

验证 _bargein_play_min_rms(conn, base_rms)：
  1. 无 config / 空 config                -> fixed 默认 700（零回归）
  2. fixed + bargein_human_min_rms_playing -> 读该固定值
  3. adaptive 全局 + base                 -> max(mult*base, floor)
  4. adaptive + base<=0                   -> 回落 floor（不被基线 0 卡死）
  5. per-device mode 覆盖                 -> 仅目标设备 adaptive，其余 fixed
  6. per-device floor 覆盖                -> 低绝对下限按设备可调
  7. 比值语义：floor 主导(小 base) / mult 主导(大 base)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuguoai_core.handle.receiveAudioHandle import _bargein_play_min_rms as F


class _Conn:
    def __init__(self, config=None, device_id="testdev"):
        self.config = config
        self.device_id = device_id


def test_no_config_returns_fixed_default():
    assert F(_Conn(config=None), 50.0) == 700.0
    print("PASS[1] no_config -> 700")


def test_empty_config_fixed_default():
    assert F(_Conn(config={}), 50.0) == 700.0
    print("PASS[1b] empty_config -> 700")


def test_fixed_reads_configured_value():
    c = _Conn(config={"bargein_human_min_rms_playing": 500})
    assert F(c, 50.0) == 500.0
    print("PASS[2] fixed configured -> 500")


def test_adaptive_global_floor_dominates_small_base():
    c = _Conn(config={
        "bargein_play_min_rms_mode": "adaptive",
        "bargein_play_min_rms_adaptive_mult": 2.5,
        "bargein_play_min_rms_adaptive_floor": 150,
    })
    # base=50 -> 2.5*50=125 < 150 -> floor 150
    assert F(c, 50.0) == 150.0
    print("PASS[3] adaptive small base -> floor 150")


def test_adaptive_mult_dominates_large_base():
    c = _Conn(config={
        "bargein_play_min_rms_mode": "adaptive",
        "bargein_play_min_rms_adaptive_mult": 2.5,
        "bargein_play_min_rms_adaptive_floor": 150,
    })
    # base=200 -> 2.5*200=500 > 150 -> 500（随噪声基线抬高，符合"越吵越严"）
    assert F(c, 200.0) == 500.0
    print("PASS[3b] adaptive large base -> mult 500")


def test_adaptive_base_zero_returns_floor():
    c = _Conn(config={
        "bargein_play_min_rms_mode": "adaptive",
        "bargein_play_min_rms_adaptive_floor": 150,
    })
    assert F(c, 0.0) == 150.0
    assert F(c, -1.0) == 150.0
    print("PASS[4] adaptive base<=0 -> floor")


def test_per_device_mode_override():
    cfg = {
        "bargein_play_min_rms_mode": "fixed",  # 全局 fixed（零回归）
        "bargein_play_min_rms_mode_per_device": {"a0f2628de2fc": "adaptive"},
        "bargein_play_min_rms_adaptive_floor": 150,
        "bargein_play_min_rms_adaptive_mult": 2.5,
    }
    p4 = _Conn(config=cfg, device_id="a0f2628de2fc")
    other = _Conn(config=cfg, device_id="94a9901a8818")
    assert F(p4, 50.0) == 150.0          # P4 -> adaptive floor
    assert F(other, 50.0) == 700.0       # 其他设备 -> fixed 700，零影响
    print("PASS[5] per-device mode override (P4 adaptive / other fixed)")


def test_per_device_floor_override():
    cfg = {
        "bargein_play_min_rms_mode": "adaptive",
        "bargein_play_min_rms_adaptive_mult": 2.5,
        "bargein_play_min_rms_adaptive_floor": 150,
        "bargein_play_min_rms_adaptive_floor_per_device": {"a0f2628de2fc": 90},
    }
    p4 = _Conn(config=cfg, device_id="a0f2628de2fc")
    # base=20 -> 2.5*20=50 < per-device floor 90 -> 90（按设备可把低绝对下限调更低）
    assert F(p4, 20.0) == 90.0
    print("PASS[6] per-device floor override -> 90")


if __name__ == "__main__":
    test_no_config_returns_fixed_default()
    test_empty_config_fixed_default()
    test_fixed_reads_configured_value()
    test_adaptive_global_floor_dominates_small_base()
    test_adaptive_mult_dominates_large_base()
    test_adaptive_base_zero_returns_floor()
    test_per_device_mode_override()
    test_per_device_floor_override()
    print("ALL PASS")
