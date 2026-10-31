"""最小化本检：桩 fuguoai_core.utils.util，导入真实 textMessageType + v30MessageHandler，
确认 v3.0 新 handler 的 message_type 值正确、模块干净导入。"""
import sys, types

# 桩 fuguoai_core.utils.util（仅 normalize_device_id 被 v30MessageHandler 用到）
_util = types.ModuleType("fuguoai_core.utils.util")
_util.normalize_device_id = lambda x: x
sys.modules["fuguoai_core.utils.util"] = _util
_utils_pkg = types.ModuleType("fuguoai_core.utils")
_utils_pkg.util = _util
sys.modules["fuguoai_core.utils"] = _utils_pkg

try:
    from fuguoai_core.handle.textMessageType import TextMessageType
    from fuguoai_core.handle.textHandler.v30MessageHandler import (
        AuthTextMessageHandler,
        SchedulePushTextMessageHandler,
        ClockTextMessageHandler,
        IntercomReadyTextMessageHandler,
    )
except Exception:
    import traceback; traceback.print_exc(); sys.exit(1)

checks = [
    ("AuthTextMessageHandler", AuthTextMessageHandler, "auth"),
    ("SchedulePushTextMessageHandler", SchedulePushTextMessageHandler, "schedule_push"),
    ("ClockTextMessageHandler", ClockTextMessageHandler, "clock"),
    ("IntercomReadyTextMessageHandler", IntercomReadyTextMessageHandler, "intercom_ready"),
]
ok = True
for name, cls, want in checks:
    got = cls().message_type.value
    good = got == want
    ok = ok and good
    print(f"  {name:30} -> {got} {'OK' if good else 'BAD(expected '+want+')'}")
# 确认枚举新成员齐全
for m in ["AUTH","START_LISTENING","STOP_LISTENING","WAKE_WORD_DETECTED","ABORT_SPEAKING","PONG","SCHEDULE_PUSH","SCHEDULE_MIRROR","INTERCOM_READY","CLOCK"]:
    good = hasattr(TextMessageType, m)
    ok = ok and good
    print(f"  enum {m:20} -> {'OK' if good else 'MISSING'}")
print("ALL_OK" if ok else "SOME_BAD")
