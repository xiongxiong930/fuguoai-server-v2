#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⑪ 动态 system prompt 渲染 memo 真进化回归测试（离线，门禁 L3）

钉住 fuguoai_core/utils/dialogue.py::_render_dynamic_part 的不可回退性质：
  1. 等价性：memo 命中/未命中的输出与旧内联逐次渲染字节级一致
     （时间替换、<memory> re.sub、说话人拼接、voiceprint_config=None 容错）。
  2. 命中率：同分钟同输入连续 N 轮只渲染 1 次（_dyn_render_count 钉住）。
  3. 失效正确：memory/说话人/模板任一变化必须重新渲染且结果正确。
  4. 完整链路：get_llm_dialogue_with_memory 输出结构不变
     （静态/fewshot/动态/历史 四段顺序与角色）。

用法：regress venv 下 `python test_dialogue_dyn_cache.py`，退出码非 0 禁止合并。
"""
import os
import re
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils.dialogue import Dialogue, Message

_results = []


def check(name, cond, detail=""):
    _results.append((name, cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


TEMPLATE = (
    "<context>\n现在时间 {{current_time}}。\n"
    "<memory>\n(空)\n</memory>\n请自然对话。"
)


def _legacy_render(dynamic_part, memory_str, voiceprint_config):
    """旧内联实现的逐行复刻，作为等价性对照真值。"""
    dynamic_part = dynamic_part.replace(
        "{{current_time}}", datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
    )
    if memory_str is not None:
        dynamic_part = re.sub(
            r"<memory>.*?</memory>",
            f"<memory>\n{memory_str}\n</memory>",
            dynamic_part,
            flags=re.DOTALL,
        )
    try:
        speakers = voiceprint_config.get("speakers", [])
        if speakers:
            dynamic_part += "\n<speakers_info>"
            for speaker_str in speakers:
                try:
                    parts = speaker_str.split(",", 2)
                    if len(parts) >= 2:
                        name = parts[1].strip()
                        description = parts[2].strip() if len(parts) >= 3 else ""
                        dynamic_part += f"\n- {name}：{description}"
                except:
                    pass
            dynamic_part += "\n</speakers_info>"
    except:
        pass
    return dynamic_part


def test_equivalence():
    cases = [
        (None, None),
        ("用户喜欢摇滚乐", None),
        ("用户喜欢摇滚乐", {"speakers": ["id1,小明,爸爸", "id2,小红"]}),
        (None, {"speakers": []}),
        ("", {"speakers": ["bad_format"]}),
    ]
    ok = True
    for mem, vp in cases:
        d = Dialogue()
        got = d._render_dynamic_part(TEMPLATE, mem, vp)
        want = _legacy_render(TEMPLATE, mem, vp)
        if got != want:
            ok = False
            check("等价性", False, f"mem={mem!r} vp={vp!r}\n got={got!r}\nwant={want!r}")
            break
    if ok:
        check("等价性-5组输入与旧实现字节级一致", True)


def test_memo_hit():
    d = Dialogue()
    outs = {d._render_dynamic_part(TEMPLATE, "记忆A", None) for _ in range(50)}
    check(
        "同分钟同输入50轮只渲染1次",
        d._dyn_render_count == 1 and len(outs) == 1,
        f"render_count={d._dyn_render_count}",
    )


def test_invalidation():
    d = Dialogue()
    r1 = d._render_dynamic_part(TEMPLATE, "记忆A", None)
    r2 = d._render_dynamic_part(TEMPLATE, "记忆B", None)
    r3 = d._render_dynamic_part(TEMPLATE, "记忆B", {"speakers": ["id,小明,哥哥"]})
    check(
        "记忆/说话人变化必重渲染且内容正确",
        d._dyn_render_count == 3
        and "记忆A" in r1
        and "记忆B" in r2
        and "小明" in r3
        and "小明" not in r2,
        f"render_count={d._dyn_render_count}",
    )


def test_full_dialogue_structure():
    d = Dialogue()
    d.put(Message(role="system", content="你是助手。" + TEMPLATE))
    d.put(Message(role="user", content="你好"))
    d.put(Message(role="assistant", content="你好呀"))
    out = d.get_llm_dialogue_with_memory("记忆X", None)
    roles = [m["role"] for m in out]
    check(
        "四段结构不变(静态sys+动态sys+历史)",
        roles == ["system", "system", "user", "assistant"]
        and out[0]["content"] == "你是助手。"
        and "记忆X" in out[1]["content"]
        and "{{current_time}}" not in out[1]["content"],
        f"roles={roles}",
    )


if __name__ == "__main__":
    test_equivalence()
    test_memo_hit()
    test_invalidation()
    test_full_dialogue_structure()
    failed = [n for n, c in _results if not c]
    if failed:
        print(f"\nFAILED: {failed}")
        sys.exit(1)
    print(f"\nALL {len(_results)} PASS")
