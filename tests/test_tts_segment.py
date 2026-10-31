#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TTS 断句回归测试（P0-2，部署前必跑）

锁定 core/providers/tts/base.py::_get_segment_text 的契约，防止再现
2026-07-12 那种"对话优化把 return '' 改成 return None → TTS 断句死循环 /
标点后文字滞留 → 设备连上不返回、服务器零报错"的静默故障。

契约（来自已回滚的正确版 base.py）：
  1. 纯标点 / 空段               -> 返回 ""  (调用方 continue，继续抽下一段)
  2. 缓冲抽干 / 暂停点过于靠前   -> 返回 None(调用方 break，退出内层抽取循环)
  3. 普通句子                    -> 返回去标点后的文本，processed_chars 前移
  4. 长串无标点超窗口            -> 返回预刷新分段(不在 <6 字处硬切，不无限等)

关键反向用例(1)专门 catch 今天那个 bug：纯标点输入若返回 None 而非 ''，
说明坏逻辑又上线了 —— 必须 FAIL。

用法（在容器内 /opt/fuguoai-server 下运行）：
    python3 test_tts_segment.py
退出码非 0 即存在断句契约破坏，禁止部署。
"""
import sys
import time

sys.path.insert(0, "/opt/fuguoai-server")

from fuguoai_core.providers.tts.base import TTSProviderBase


class _StubTTS(TTSProviderBase):
    """最小子类：仅需实现抽象方法 text_to_speak，本测试不触发合成。"""

    async def text_to_speak(self, text, output_file):
        return b""


def make_provider():
    p = _StubTTS(config={}, delete_audio_file=True)
    # 重置断句状态（__init__ 已设初值，这里再显式清一遍，确保用例隔离）
    p.tts_text_buff = []
    p.processed_chars = 0
    p.is_first_sentence = True
    p.tts_stop_request = False
    p.tts_segment_max_chars = 45
    return p


def feed(p, text):
    p.tts_text_buff.append(text)


_results = []


def check(name, cond, detail=""):
    _results.append((name, cond, detail))
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def test_pure_punct_returns_empty_not_none():
    """核心回归：纯标点段必须返回 '' 而非 None（今天 bug 根因）"""
    p = make_provider()
    feed(p, "？")
    r = p._get_segment_text()
    check(
        "纯标点'? '返回空串''而非None",
        r == "",
        f"实际返回 {r!r}",
    )


def test_pure_punct_in_delim_set_returns_empty():
    """断句标点集内的纯标点(。！？；：)必须返回 '' 而非 None。
    这是 2026-07-12 故障根因的反向用例。逗号/顿号不在断句集，
    本就返回 None(继续累积)，属设计预期，不在此测。"""
    for punct in ["。", "！", "？", "；", "："]:
        p = make_provider()
        feed(p, punct)
        r = p._get_segment_text()
        check(f"纯断句标点'{punct}'返回空串''而非None", r == "", f"实际返回 {r!r}")


def test_normal_sentence():
    p = make_provider()
    feed(p, "你好呀。")
    r = p._get_segment_text()
    check("正常句'你好呀。'返回'你好呀'", r == "你好呀", f"实际返回 {r!r}")
    check("processed_chars 已前移", p.processed_chars == len("你好呀。"),
          f"processed_chars={p.processed_chars}")


def test_two_sentences_streamed():
    """模拟 LLM 流式分两条 MIDDLE 消息喂入，验证游标正确推进"""
    p = make_provider()
    feed(p, "今天天气真好。")
    s1 = p._get_segment_text()
    check("第一条抽到'今天天气真好'", s1 == "今天天气真好", f"实际 {s1!r}")
    feed(p, "我们去公园吧！")
    s2 = p._get_segment_text()
    check("第二条抽到'我们去公园吧'", s2 == "我们去公园吧", f"实际 {s2!r}")
    s3 = p._get_segment_text()
    check("缓冲抽干返回 None", s3 is None, f"实际 {s3!r}")


def test_buffer_drained_returns_none():
    p = make_provider()
    r = p._get_segment_text()
    check("空缓冲返回 None", r is None, f"实际 {r!r}")


def test_long_no_punct_prefetch():
    p = make_provider()
    p.tts_segment_max_chars = 10
    longtext = "这是一个非常长的没有任何标点符号的句子用来测试预刷新窗口切分"
    feed(p, longtext)
    r = p._get_segment_text()
    check("长串无标点返回预刷新分段(非空且<=窗口)",
          r is not None and 0 < len(r) <= 10,
          f"实际 {r!r} len={len(r) if r else 0}")


def test_pause_too_early_returns_none():
    """窗口内最长停顿点 <6 字时，宁可等待返回 None，不硬切劈词"""
    p = make_provider()
    p.tts_segment_max_chars = 45
    # 前 6 字内无自然停顿，但超过窗口阈值，触发预刷新；
    # 这里构造一个窗口内停顿点过于靠前的场景：
    # "武汉" + 长串无标点，窗口45内最近停顿在最前 -> cut<6 -> 返回 None
    p.tts_segment_max_chars = 8
    feed(p, "武汉长江大桥是一座历史名桥位于长江之上建成于一九五七年")
    # "武汉"后才有内容，窗口8内"武汉"无逗号，最近停顿在更后 -> cut 可能<6
    r = p._get_segment_text()
    # 要么返回合理分段，要么返回 None（宁可等），绝不应 return 导致死循环
    check("长串预刷新不硬切劈词(cut<6 时返回 None 或有效段)",
          r is None or (isinstance(r, str) and len(r) >= 2),
          f"实际 {r!r}")


def test_no_deadloop_on_repeated_calls():
    """反复调用直到 None，确认不会死循环（给个超时保护）"""
    p = make_provider()
    feed(p, "你好。")
    start = time.time()
    n = 0
    while True:
        r = p._get_segment_text()
        n += 1
        if r is None:
            break
        if time.time() - start > 5:
            check("无死循环", False, "调用疑似死循环(>5s)")
            return
    check("无死循环(重复调用能在有限步内收敛)", n <= 5, f"调用次数 n={n}")


def test_first_sentence_comma_boundary_flushes_early():
    """杠杆 A：首句在第一个逗号处提前 flush，而非等整句末句号"""
    p = make_provider()
    feed(p, "今天天气真好，我们出去走走吧。")
    r = p._get_segment_text()
    check("首句遇逗号提前 flush 为'今天天气真好'", r == "今天天气真好", f"实际 {r!r}")
    # 其余部分在下一轮按强边界抽取
    s2 = p._get_segment_text()
    check("首句后剩余按句号抽取'我们出去走走吧'", s2 == "我们出去走走吧", f"实际 {s2!r}")


def test_first_sentence_short_comma_waits_for_period():
    """杠杆 A 安全护栏：首个逗号过于靠前(<min)时仍等强边界，避免碎段"""
    p = make_provider()
    p.first_comma_min_seg = 4
    feed(p, "嗯，好的。")
    r = p._get_segment_text()
    check("短首句逗号靠前后仍等句号'嗯，好的'", r == "嗯，好的", f"实际 {r!r}")


def test_first_comma_boundary_disabled_uses_period():
    """开关关闭时回退原行为：首句等整句末句号"""
    p = make_provider()
    p.first_comma_boundary = False
    feed(p, "今天天气真好，我们出去走走吧。")
    r = p._get_segment_text()
    check(
        "关闭逗号边界时首句等句号(整句逗号保留在内部)",
        r == "今天天气真好，我们出去走走吧",
        f"实际 {r!r}",
    )


def main():
    test_pure_punct_returns_empty_not_none()
    test_pure_punct_in_delim_set_returns_empty()
    test_normal_sentence()
    test_two_sentences_streamed()
    test_buffer_drained_returns_none()
    test_long_no_punct_prefetch()
    test_pause_too_early_returns_none()
    test_no_deadloop_on_repeated_calls()
    test_first_sentence_comma_boundary_flushes_early()
    test_first_sentence_short_comma_waits_for_period()
    test_first_comma_boundary_disabled_uses_period()

    failed = [r for r in _results if not r[1]]
    print()
    if failed:
        print(f"❌ {len(failed)}/{len(_results)} 用例失败：")
        for name, _, detail in failed:
            print(f"   - {name}  {detail}")
        sys.exit(1)
    print(f"✅ 全部 {len(_results)} 个 TTS 断句回归用例通过")


if __name__ == "__main__":
    main()
