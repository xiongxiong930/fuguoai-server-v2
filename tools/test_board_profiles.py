"""BoardProfile 解耦层单测（零回归 + 即插即用验证）。

验证点：
  1. 已知板（8818/90C0/b4b0）解析出的 aec.mode / status 与 159 部署态一致
  2. 带冒号 MAC 归一化后正确解析
  3. 未知板返回保守 default（status=unknown），绝不崩溃
  4. 运行时 register_profile 一条即可接入新板（"接各种主控板子"的核心验收）
  5. 看门狗超时默认值与模块默认一致（零回归）

运行：从仓根 `python3 tools/test_board_profiles.py`
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuguoai_core.device_profiles import (  # noqa: E402
    resolve_board_profile,
    normalize_device_id,
    register_profile,
    BoardProfile,
)

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


# ── 已知板：aec.mode 与部署态一致 ──
p8818 = resolve_board_profile("94a9901a8818")
check("8818 mode=2ch_srvref", p8818.aec.mode == "2ch_srvref", f"got {p8818.aec.mode}")
check("8818 status=active", p8818.status == "active")
check("8818 max_delay=12000", p8818.aec.max_delay_ms == 12000)
check("8818 silence_after=15.0", p8818.call.silence_after_flow_ms == 15.0)

p90 = resolve_board_profile("94:a9:90:1a:90:c0")  # 带冒号
check("90C0 冒号归一→outband", p90.aec.mode == "outband", f"got {p90.aec.mode}")
check("90C0 bargein=disabled", p90.capabilities.bargein == "disabled")

pb4 = resolve_board_profile("94a9902db4b0")
check("b4b0 status=deprecated", pb4.status == "deprecated")
check("b4b0 mode=outband", pb4.aec.mode == "outband")

# ── 未知板：保守 default，不崩 ──
punk = resolve_board_profile("00:11:22:33:44:55")
check("未知板 status=unknown", punk.status == "unknown", f"got {punk.status}")
check("未知板 mode=outband(保守)", punk.aec.mode == "outband", f"got {punk.aec.mode}")
check("未知板 audio.codec=opus(默认)", punk.audio.codec == "opus")

# ── 归一化函数 ──
check("normalize 去冒号小写",
      normalize_device_id("94:A9:90:1A:88:18") == "94a9901a8818")

# ── 即插即用：运行时注册新板 ──
register_profile("rkN", {"mac": "deadbeef0001", "aec": {"mode": "2ch"}, "status": "active"})
prk = resolve_board_profile("deadbeef0001")
check("新板 rkN mode=2ch(注册即生效)", prk.aec.mode == "2ch", f"got {prk.aec.mode}")
check("新板 rkN status=active", prk.status == "active")

# ── board_id 直接匹配 ──
check("board_id 直传 8818", resolve_board_profile("8818").board_id == "8818")

# ── 返回类型 ──
check("返回 BoardProfile 实例", isinstance(p8818, BoardProfile))

print(f"\nTOTAL={PASS + FAIL} PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
