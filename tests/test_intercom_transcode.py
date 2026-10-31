#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对讲转码契约回归（P0，部署前必跑）—— 锁定 intercom_registry._transcode 的双向契约。

背景：
- 2026-07-28 的 60ms 实验(f687e20)把手机→设备编码从 20ms/640B 改成 60ms/1920B，
  导致设备上行帧率塌到 14.6fps(0.29×)。回退(f2096b8)后用本测试把 20ms 契约钉死。
- 2026-08-02 固件移除 intercom 4 字节 seq 头（契约 §B.9/§B.13）：设备↔手机全部裸 opus
  透传，relay 不再拼/剥任何 seq 头。旧 seq 逻辑是「对话正常、对讲双端杂音」的根因——
  设备侧发送裸 opus 时，relay 仍剥 4B 会吃掉真实 opus 首部、拼 4B 会让设备收到脏头。
  本测试即锁定「裸 opus 透传、零 seq」这一修订。

契约（v1.6.0 Part B，2026-08-01 修订）：
  手机→设备(pcm 模式)：N×640B 裸 PCM → N 个裸 opus 帧(320样本/20ms，无 seq 头)；
    seq 逻辑已移除；尾料(<640B)丢弃；每 640B 输入产出恰 1 帧。
  设备→手机(pcm 模式)：裸 opus 净荷 → 640B 裸 PCM（无任何头，不再剥 4B seq）。
  同类型(设备↔设备)：原样透传（不转码、不改字节）。

证明策略：以「裸 opus 解码能正确解回 640B/20ms 非静音 PCM」作为「无 seq 头」的判据——
若仍拼/剥 4B，解码要么抛错、要么得到残缺数据（长度≠640 或 RMS 异常），测试必 FAIL。

用法： <python> test_intercom_transcode.py   （无网络/无容器依赖）
"""
import math
import struct
import sys

sys.path.insert(0, ".")

from fuguoai_core.utils.intercom_registry import IntercomRegistry  # noqa: E402
import opuslib_next  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def sine_pcm(n_samples=320, freq=440, amp=8000):
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / 16000)))
        for i in range(n_samples)
    )


def rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    vals = struct.unpack(f"<{n}h", pcm[: n * 2])
    return math.sqrt(sum(v * v for v in vals) / n) / 32768.0


def main():
    reg = IntercomRegistry()
    phone, dev = "phone-test01", "94:a9:90:1a:88:18"

    # ── 1. 手机→设备：单 640B PCM → 1 个裸 opus 帧(无 seq) ──
    pcm = sine_pcm(320)
    out = reg._transcode(pcm, phone, dev, "pcm")
    check("1a 单帧非空", out is not None and len(out) > 0)
    # 关键回归：裸 opus 才能被直接解码；若仍拼 4B seq 头 → 解码抛错或长度≠640
    dec = opuslib_next.Decoder(16000, 1)
    back = dec.decode(bytes(out), 320)
    check("1b opus 解回 640B/20ms(无 seq 头才能解)", len(back) == 640, f"got {len(back)}")
    check("1c 解回音频非静音", rms(back) > 0.05, f"rms={rms(back):.4f}")

    # ── 2. 手机→设备：1920B(3×640) → 约 3 个裸 opus 帧 ──
    single_len = len(out)
    out2 = reg._transcode(sine_pcm(960), phone, dev, "pcm")
    # 多帧拼接总长度应明显大于单帧（证明产出了多帧而非 1 帧）
    check("2a 多帧拼接>单帧", len(out2) > single_len * 1.5,
          f"len2={len(out2)} single={single_len}")
    back2 = dec.decode(bytes(out2), 320)
    check("2b 多帧首帧解回 640B/20ms", len(back2) == 640, f"got {len(back2)}")

    # ── 3. 尾料丢弃：帧数比例法（opus 帧长随编码状态变化，不能比绝对长度）──
    # 640B → 1 帧；1280B(=2×640) → 2 帧；1000B(640+360尾料) → 尾料被丢弃 → 仍 1 帧。
    out_640 = reg._transcode(sine_pcm(320), phone, dev, "pcm")                  # 1 帧
    out_1280 = reg._transcode(sine_pcm(640), phone, dev, "pcm")                # 2 帧
    out_1000 = reg._transcode(sine_pcm(320) + b"\x00" * 360, phone, dev, "pcm")  # 尾料丢弃=1 帧
    check("3a 1280B=2帧(产出>1.5×单帧)",
          out_1280 is not None and len(out_1280) > len(out_640) * 1.5,
          f"1280={len(out_1280)} 640={len(out_640)}")
    check("3b 1000B尾料丢弃=1帧(产出<0.75×双帧)",
          out_1000 is not None and len(out_1000) < len(out_1280) * 0.75,
          f"1000={len(out_1000)} 1280={len(out_1280)}")

    # ── 4. 设备→手机：裸 opus → 640B 裸 PCM 无头（本 regression 核心：旧逻辑剥 4B→杂音）──
    enc = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_VOIP)
    op = enc.encode(sine_pcm(320), 320)
    down = reg._transcode(op, dev, phone, "pcm")   # 注意：传入裸 opus，不再带 seq
    check("4a 下行输出 640B", down is not None and len(down) == 640,
          f"got {None if down is None else len(down)}")
    check("4b 下行确为解码后的 PCM(非原样透传 opus)", down != op)
    check("4c 下行解码非静音(不剥首部才能正确)", rms(down) > 0.05, f"rms={rms(down):.4f}")

    # ── 5. 边界：过小下行帧 → None；不足 640B 上行 → None ──
    check("5a 下行<4B 返回 None", reg._transcode(b"\x00\x01", dev, phone, "pcm") is None)
    check("5b 上行<640B 返回 None", reg._transcode(b"\x00" * 100, phone, dev, "pcm") is None)

    # ── 6. 同类型透传：设备↔设备原样字节 ──
    raw = b"\x00\x00\x00\x2a" + op
    check("6  设备↔设备透传不改字节", reg._transcode(raw, dev, "aa:bb:cc:dd:ee:ff", "pcm") == raw)

    # ── 7. seq 状态已被移除（不再残留 seq 字段）──
    codec = reg._phone_codecs.get(phone)
    check("7  codec 无 seq 字段(已弃用)", codec is None or "seq" not in codec)

    print()
    if FAILS:
        print(f"结果: {len(FAILS)} 项 FAIL ❌ — 对讲转码契约被破坏，禁止部署: {FAILS}")
        return 1
    print("结果: ALL PASS ✅ (20ms/640B 裸 opus 契约完好，无 seq 头)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
