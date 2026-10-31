"""离线测试：intercom_stats（计数正确性 / 方向判定 / 滑窗 / 快照留存 / 异常自吞）。

纯离线，零依赖。纳入 tools/regression_gate.py L3。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils.intercom_stats import (
    IntercomStatsHub, RECENT_KEEP, STALL_SEC,
)

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def main():
    print("== phone-device 会话方向判定 ==")
    hub = IntercomStatsHub()
    hub.session_start("sid1", "phone-u1", "dev-a")
    hub.note_frames("phone-u1", 3, 300)   # 手机→设备 = up（一次 relay 3 帧）
    hub.note_frames("dev-a", 1, 100)      # 设备→手机 = down
    hub.note_frames("dev-a", 1, 100)
    act = hub.active()
    check("活跃会话 1 个", len(act) == 1)
    s = act[0]
    check("mode=phone-device", s["mode"] == "phone-device")
    check("up.frames=3", s["up"]["frames"] == 3)
    check("down.frames=2", s["down"]["frames"] == 2)
    check("up.bytes=300", s["up"]["bytes"] == 300)

    print("== 错误计数 ==")
    hub.note_err("phone-u1")
    hub.note_err("dev-a")
    hub.note_err("dev-a")
    s = hub.active()[0]
    check("up.errs=1", s["up"]["errs"] == 1)
    check("down.errs=2", s["down"]["errs"] == 2)

    print("== device-device 方向（a→b=up, b→a=down） ==")
    hub2 = IntercomStatsHub()
    hub2.session_start("sid2", "dev-x", "dev-y")
    hub2.note_frames("dev-x", 1, 80)
    hub2.note_frames("dev-y", 1, 80)
    s2 = hub2.active()[0]
    check("mode=device-device", s2["mode"] == "device-device")
    check("a→b 计入 up", s2["up"]["frames"] == 1)
    check("b→a 计入 down", s2["down"]["frames"] == 1)

    print("== 会话结束 → recent 快照 ==")
    hub.session_end("sid1", "hangup")
    check("结束后 active 空", hub.active() == [])
    rec = hub.recent()
    check("recent 有 1 条", len(rec) == 1)
    check("快照 end_reason=hangup", rec[0].get("end_reason") == "hangup")
    check("快照保留帧数", rec[0]["up"]["frames"] == 3)

    print("== recent 上限淘汰 ==")
    hub3 = IntercomStatsHub()
    for i in range(RECENT_KEEP + 5):
        sid = f"s{i}"
        hub3.session_start(sid, f"phone-{i}", f"dev-{i}")
        hub3.session_end(sid, "hangup")
    check(f"recent 封顶 {RECENT_KEEP}", len(hub3.recent(99)) == RECENT_KEEP)

    print("== 滑窗 fps ==")
    hub4 = IntercomStatsHub()
    hub4.session_start("sw", "phone-z", "dev-z")
    for _ in range(100):
        hub4.note_frames("phone-z", 1, 100)
    s4 = hub4.active()[0]
    # 当前桶不足 1s 时可能不出数——只验字段类型与非负
    check("fps_10s 是 list", isinstance(s4["up"]["fps_10s"], list))
    check("stall 初始 False", s4["up"]["stall"] is False)

    print("== stall 判定 ==")
    hub5 = IntercomStatsHub()
    hub5.session_start("st", "phone-s", "dev-s")
    hub5.note_frames("phone-s", 1, 100)
    # 手工把 last_ts 拨老
    with hub5._lock:
        hub5._by_sid["st"].up.last_ts = time.time() - STALL_SEC - 1
    check("超时判 stall", hub5.active()[0]["up"]["stall"] is True)

    print("== 未注册来源/重复结束：静默无异常 ==")
    try:
        hub.note_frames("ghost-dev", 1, 100)
        hub.note_err("ghost-dev")
        hub.session_end("sid1", "again")
        hub.session_end("never-existed", "x")
        check("异常自吞", True)
    except Exception:
        check("异常自吞", False)

    print(f"\n结果: {PASS} PASS / {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
