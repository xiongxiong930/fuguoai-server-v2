#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⑨ TTS 增量拼接真进化回归测试（离线，门禁 L3）

钉住 fuguoai_core/providers/tts/base.py::_joined_full_text 的两条不可回退性质：
  1. 等价性：任意 append/整体替换/收缩序列下，增量缓存结果与全量
     "".join(tts_text_buff) 字节级一致（含 FIRST 重置、测试外部直接赋值场景）。
  2. 复杂度：n 块流式输入逐块调用 _get_segment_text，累计 join 字符量
     _join_work_chars 必须 O(总字符数)（旧全量 join 为 O(n²)，会超阈值 FAIL）。

用法：regress venv 下 `python test_tts_join_evolution.py`，退出码非 0 禁止合并。
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.providers.tts.base import TTSProviderBase


class _StubTTS(TTSProviderBase):
    async def text_to_speak(self, text, output_file):
        return b""


def make_provider():
    p = _StubTTS(config={}, delete_audio_file=True)
    p.tts_text_buff = []
    p.processed_chars = 0
    p.is_first_sentence = True
    p.tts_stop_request = False
    p.tts_segment_max_chars = 45
    return p


_results = []


def check(name, cond, detail=""):
    _results.append((name, cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def test_equivalence_random_ops():
    """500 步随机 append/替换/收缩，增量缓存与全量 join 逐步全等。"""
    rng = random.Random(20260729)
    p = make_provider()
    ok = True
    for step in range(500):
        op = rng.random()
        if op < 0.75:
            p.tts_text_buff.append(
                "".join(rng.choice("你好世界abc，。！？123 ") for _ in range(rng.randint(0, 20)))
            )
        elif op < 0.90:
            # FIRST 重置 / 外部整体替换
            p.tts_text_buff = [
                "".join(rng.choice("测试xyz。") for _ in range(rng.randint(0, 10)))
                for _ in range(rng.randint(0, 3))
            ]
        else:
            # 收缩（同对象 in-place 删块）
            del p.tts_text_buff[rng.randint(0, len(p.tts_text_buff)) :]
        got = p._joined_full_text()
        want = "".join(p.tts_text_buff)
        if got != want:
            ok = False
            check("等价性-随机序列", False, f"step={step} got={got!r} want={want!r}")
            break
    if ok:
        check("等价性-500步随机append/替换/收缩全等", True)


def test_join_work_is_linear():
    """n 块逐块喂入并调用 _get_segment_text，累计 join 量须 O(总字符)。

    构造无标点长串（走 None/预刷新分支，最大化调用次数）：
    n=800 块 × 10 字符 = 8000 字符。旧全量 join 累计 ≈ 8000*800/2 = 3.2M；
    新增量实现累计 = 8000 + 少量重建。阈值 60000（余量 >7x，旧实现超 50x）。
    """
    p = make_provider()
    p.tts_segment_max_chars = 10 ** 9  # 关预刷新，确保每次都全文扫描
    n, chunk = 800, "无标点长串内容块"  # 8 字符，无断句标点
    for _ in range(n):
        p.tts_text_buff.append(chunk)
        p._get_segment_text()
    total_chars = n * len(chunk)
    work = p._join_work_chars
    check(
        "复杂度-累计join字符量O(n)",
        work <= max(60000, total_chars * 8),
        f"total={total_chars} work={work} (旧O(n²)实现约{total_chars * n // 2})",
    )


def test_segment_semantics_after_reset():
    """FIRST 式重置(整体替换新list)后，分段语义与全新 provider 一致。"""
    p = make_provider()
    p.tts_text_buff.append("第一轮内容。")
    seg1 = p._get_segment_text()
    # 模拟 FIRST 重置（tts_text_priority_thread:630 的写法）
    p.processed_chars = 0
    p.tts_text_buff = []
    p.is_first_sentence = True
    p.tts_text_buff.append("第二轮新内容！")
    seg2 = p._get_segment_text()
    check(
        "重置后分段语义正确",
        seg1 == "第一轮内容" and seg2 == "第二轮新内容",
        f"seg1={seg1!r} seg2={seg2!r}",
    )


if __name__ == "__main__":
    test_equivalence_random_ops()
    test_join_work_is_linear()
    test_segment_semantics_after_reset()
    failed = [n for n, c in _results if not c]
    if failed:
        print(f"\nFAILED: {failed}")
        sys.exit(1)
    print(f"\nALL {len(_results)} PASS")
