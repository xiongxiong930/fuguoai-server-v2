#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0-1 EdgeTTS 超时加固验证（容器内运行）：
  1. real_synth: 真实调用 EdgeTTS 合成，确认正常路径不被破坏（应 1~3s 返回音频）。
  2. hang_timeout: mock 微软 stream 永久挂起，确认 wait_for 在 tts_timeout(2s) 内
     触发超时异常（不再无限挂死）。

用法（容器内 /opt/fuguoai-server 下）：
    python3 test_edge_timeout.py
"""
import asyncio
import sys
import time

sys.path.insert(0, "/opt/fuguoai-server")

import edge_tts
from unittest.mock import patch
from fuguoai_core.providers.tts.edge import TTSProvider


class HangStream:
    """模拟微软 speech 服务卡死：迭代永不返回。"""

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)


class FakeComm:
    def __init__(self, *a, **k):
        pass

    def stream(self):
        return HangStream()

    async def close(self):
        print("  [mock] communicate.close() called")


async def real_synth():
    p = TTSProvider(config={"voice": "zh-CN-XiaoxiaoNeural"}, delete_audio_file=True)
    t0 = time.time()
    try:
        b = await asyncio.wait_for(p.text_to_speak("你好呀，今天天气不错。", None), timeout=30)
        dt = time.time() - t0
        assert b and len(b) > 100, f"音频过短: {len(b) if b else 0}"
        print(f"[PASS] REAL EdgeTTS 正常合成 bytes={len(b)} in {dt:.2f}s")
    except Exception as e:
        print(f"[FAIL] REAL EdgeTTS 异常 {type(e).__name__}: {e} in {time.time()-t0:.2f}s")
        raise


async def hang_timeout():
    p = TTSProvider(config={"voice": "zh-CN-XiaoxiaoNeural"}, delete_audio_file=True)
    p.tts_timeout = 2
    with patch.object(edge_tts, "Communicate", FakeComm):
        t0 = time.time()
        try:
            await p.text_to_speak("你好", None)
            print("[FAIL] HANG TEST 应该超时却成功返回了")
            raise AssertionError("expected timeout")
        except Exception as e:
            dt = time.time() - t0
            print(f"[PASS] HANG TEST 超时触发 {type(e).__name__}: {str(e)[:50]} in {dt:.1f}s")
            assert dt < 5, f"超时应在 ~2s 触发，实际 {dt:.1f}s"


async def main():
    await real_synth()
    print("---")
    await hang_timeout()
    print("\n✅ P0-1 EdgeTTS 超时加固验证通过")


if __name__ == "__main__":
    asyncio.run(main())
