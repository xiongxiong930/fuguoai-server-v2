"""离线测试：voicemail_store（fgvm 回环 / 配额 / 状态迁移 / TTL 清扫 / 转码契约）。

纯离线：不连网络、不碰容器。依赖 opuslib_next（回归 venv 已装）。
纳入 tools/regression_gate.py L3。
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils import voicemail_store as vs
from fuguoai_core.utils.voicemail_store import (
    VoicemailStore, write_fgvm, read_fgvm, pcm_to_frames, transcode_upload,
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


def make_pcm(seconds: float) -> bytes:
    """440Hz 正弦 16k/mono/s16le。"""
    import math
    import struct as st
    n = int(16000 * seconds)
    return b"".join(
        st.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 16000)))
        for i in range(n))


def main():
    tmp = tempfile.mkdtemp(prefix="vmtest_")
    store = VoicemailStore(base_dir=tmp)
    dev = "94:a9:90:1a:88:18"

    print("== fgvm 封装/解析回环 ==")
    pcm = make_pcm(1.0)
    frames = pcm_to_frames(pcm)
    check("1s 音频切出 50 帧", len(frames) == 50)
    path = os.path.join(tmp, "loop.fgvm")
    write_fgvm(path, frames)
    meta, frames2 = read_fgvm(path)
    check("meta: 16k/mono/20ms", (meta["sample_rate"], meta["channels"],
                                  meta["frame_ms"]) == (16000, 1, 20))
    check("frame_count 一致", meta["frame_count"] == 50)
    check("帧字节完全一致", frames == frames2)
    check("duration_ms=1000", meta["duration_ms"] == 1000)

    print("== 解码回环（帧可被 320 样本解出） ==")
    import opuslib_next
    dec = opuslib_next.Decoder(16000, 1)
    ok = all(len(dec.decode(fr, 320)) == 640 for fr in frames2)
    check("全部帧解码 640B", ok)

    print("== 坏文件拒收 ==")
    bad = os.path.join(tmp, "bad.fgvm")
    with open(bad, "wb") as f:
        f.write(b"NOTMAGIC" + b"\x00" * 16)
    try:
        read_fgvm(bad)
        check("bad magic 抛 ValueError", False)
    except ValueError:
        check("bad magic 抛 ValueError", True)

    print("== 转码契约 ==")
    frames3 = transcode_upload(make_pcm(2.0), "pcm")
    check("2s pcm → 100 帧", len(frames3) == 100)
    try:
        transcode_upload(make_pcm(0.05), "pcm")
        check("过短拒收 too_short", False)
    except ValueError as e:
        check("过短拒收 too_short", str(e) == "too_short")
    # 61s 超长拒收（构造静音省时）
    try:
        transcode_upload(b"\x00" * (16000 * 2 * 61), "pcm")
        check("超长拒收 too_long", False)
    except ValueError as e:
        check("超长拒收 too_long", str(e) == "too_long")

    print("== CRUD + 状态迁移 ==")
    row = store.add(dev, "phone-u1", "u1", frames, 1000, "pcm")
    check("add 返回 msg_id", row.get("msg_id", "").startswith("vm_"))
    mid = row["msg_id"]
    check("list 可见 status=new", store.list(dev)[0]["status"] == "new")
    check("pending 含该条", any(m["msg_id"] == mid for m in store.pending(dev)))
    check("set played", store.set_status(dev, mid, "played"))
    check("played 后不在 pending", not any(m["msg_id"] == mid
                                          for m in store.pending(dev)))
    check("set read", store.set_status(dev, mid, "read"))
    check("get 状态=read", store.get(dev, mid)["status"] == "read")
    store.delete(dev, mid)
    check("delete 后 list 不可见", store.get(dev, mid) is None)
    check("delete 后音频文件已删", not os.path.exists(store.audio_path(dev, mid)))

    print("== 配额 ==")
    for i in range(vs.MAX_MSGS):
        r = store.add(dev, "phone-u1", "u1", frames[:5], 100, "pcm")
        assert not r.get("error"), f"第{i}条不应拒收"
    r = store.add(dev, "phone-u1", "u1", frames[:5], 100, "pcm")
    check(f"第 {vs.MAX_MSGS + 1} 条拒收 quota_exceeded",
          r.get("error") == "quota_exceeded")

    print("== TTL 清扫 ==")
    dev2 = "dev-ttl-test"
    r = store.add(dev2, "phone-u1", "u1", frames[:5], 100, "pcm")
    # 人工把 created_at 改老：追加一行覆盖
    store._append_index(dev2, {"msg_id": r["msg_id"],
                               "created_at": int(time.time()) - 8 * 86400})
    store.cleanup(dev2)
    check("8 天前留言被清", store.get(dev2, r["msg_id"]) is None)
    check("音频文件被清", not os.path.exists(store.audio_path(dev2, r["msg_id"])))

    print("== devices_with_pending ==")
    check("dev 有 pending 在列", dev in store.devices_with_pending())
    check("dev2 无 pending 不在列", dev2 not in store.devices_with_pending())

    print(f"\n结果: {PASS} PASS / {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
