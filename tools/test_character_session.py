"""NFC 角色双交互面路由骨架冒烟测试（独立可跑，不依赖 159）。

覆盖：角色目录加载/白名单、device 面路由(设备发声)、app 面路由(设备静音)、
channel 切换、同卡幂等、clear 回默认、未知 id 拒。
运行：FUGUOAI_DATA_DIR=$(mktemp -d) python tools/test_character_session.py
"""
import os
import sys
import tempfile

# 必须在导入 character_store 前设定 data 目录（其模块级 _DATA_DIR 读取 env）
_TMP = tempfile.mkdtemp(prefix="char_test_")
os.environ["FUGUOAI_DATA_DIR"] = _TMP

from fuguoai_core.utils.character_store import (
    CharacterProfileStore, CharacterSessionStore, CharacterRouter,
    CHANNEL_DEVICE, CHANNEL_APP,
)

_YAML = """
characters:
  fox-001:
    name: 小狐
    gender: female
    age: 8
    personality: [活泼, 好奇, 傲娇]
    system_prompt: "你是小狐，一只8岁的小狐狸，说话活泼带点傲娇。"
    voice: zh-CN-XiaoxiaoNeural
  bear-002:
    name: 小熊
    gender: male
    system_prompt: "你是小熊，沉稳可靠的大哥哥。"
"""

PROFILES = os.path.join(_TMP, "characters.yaml")
SESSIONS = os.path.join(_TMP, "character_sessions.json")
with open(PROFILES, "w", encoding="utf-8") as f:
    f.write(_YAML)

FAIL = 0


def check(cond, msg):
    global FAIL
    if cond:
        print(f"  PASS  {msg}")
    else:
        FAIL += 1
        print(f"  FAIL  {msg}")


def make_router():
    ps = CharacterProfileStore(profiles_path=PROFILES)
    ss = CharacterSessionStore(sessions_path=SESSIONS)
    return CharacterRouter(ps, ss), ps, ss


print("== 1) 角色目录 ==")
r, ps, ss = make_router()
check(ps.get("fox-001") is not None, "get 已知角色 fox-001")
check(ps.get("fox-001").gender == "female", "fox-001 gender=female")
check(len(ps.list_all()) == 2, "list_all 返回 2 个角色")
check(ps.validate_id("fox-001") is True, "白名单含 fox-001")
check(ps.validate_id("evil-999") is False, "白名单拒未知 evil-999")

print("== 2) 设备碰卡 → device 面（设备发声）==")
ack = r.on_nfc_tap("uid-A", "fox-001", device_id="dev-1")
check(ack["ok"] is True, "on_nfc_tap ok")
check(ack["name"] == "小狐", "ack 回 name=小狐")
check(ack["channel"] == CHANNEL_DEVICE, "ack 含 channel=device")
check(ack["device_should_speak"] is True, "ack device_should_speak=True")
check(ss.get("uid-A").device_id == "dev-1", "device 来源落 device_id")
tgt = r.route_response("uid-A", "明天见")
check(tgt.target == CHANNEL_DEVICE, "route_response target=device")
check(tgt.device_should_speak() is True, "device_should_speak=True（设备发声）")
check(r.build_system_prompt("uid-A") == "你是小狐，一只8岁的小狐狸，说话活泼带点傲娇。",
      "build_system_prompt 注入角色提示")
check(r.resolve_voice("uid-A") == "zh-CN-XiaoxiaoNeural", "resolve_voice 取角色声线")

print("== 3) 小程序选角色 → app 面（设备静音）==")
ack2 = r.on_app_open("uid-A", "fox-001")
check(ack2["ok"] is True, "on_app_open ok")
check(ack2["channel"] == CHANNEL_APP, "on_app_open ack channel=app")
check(ack2["device_should_speak"] is False, "on_app_open ack device_should_speak=False")
tgt2 = r.route_response("uid-A", "明天见")
check(tgt2.target == CHANNEL_APP, "route_response target=app")
check(tgt2.device_should_speak() is False, "device_should_speak=False（设备静音）")
check(tgt2.text == "明天见" and tgt2.voice == "zh-CN-XiaoxiaoNeural",
      "app 面带回文本+声线供小程序消费")

print("== 4) channel 切换（latest wins）==")
r.switch_channel("uid-A", CHANNEL_DEVICE)
check(r.route_response("uid-A", "x").target == CHANNEL_DEVICE, "切回 device 面生效")

print("== 5) 同卡幂等 ==")
before = ss.get("uid-A").to_dict()
r.on_nfc_tap("uid-A", "fox-001", device_id="dev-1")
after = ss.get("uid-A").to_dict()
check(before == after, "同卡同面重复碰 = 无变化(no-op)")

print("== 6) clear 回默认 ==")
ss.clear("uid-A")
check(r.route_response("uid-A", "x").target == "none", "clear 后 target=none(无角色)")
check(r.build_system_prompt("uid-A") is None, "clear 后 system_prompt=None")

print("== 7) 未知 id 拒 ==")
bad = r.on_nfc_tap("uid-B", "evil-999")
check(bad == {"ok": False, "reason": "unknown_character"}, "未知角色被拒")
check(ss.get("uid-B") is None, "未知角色不落 session")

print("== 8) 手机 NFC 碰卡 → app 面（source=app 静音）==")
ack_app = r.on_nfc_tap("uid-C", "fox-001", source=CHANNEL_APP)
check(ack_app["ok"] is True, "on_nfc_tap(source=app) ok")
check(ack_app["channel"] == CHANNEL_APP, "source=app → channel=app")
check(ack_app["device_should_speak"] is False, "source=app → device_should_speak=False")
check(r.route_response("uid-C", "hi").target == CHANNEL_APP, "uid-C route=app（静音）")
sess_c = ss.get("uid-C")
check(sess_c.channel == CHANNEL_APP and sess_c.device_id is None, "app 来源不绑定 device_id")

print("== 9) 非法 source 拒 ==")
bad_src = r.on_nfc_tap("uid-D", "fox-001", source="web")
check(bad_src == {"ok": False, "reason": "bad_source"}, "非法 source 被拒")
check(ss.get("uid-D") is None, "非法 source 不落 session")

print("== 10) 持久化 ==")
check(os.path.exists(SESSIONS), "session 落盘 character_sessions.json")

print(f"\nTOTAL  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
