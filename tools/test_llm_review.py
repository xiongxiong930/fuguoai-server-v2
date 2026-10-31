# -*- coding: utf-8 -*-
"""文旅 KB：语义级 LLM 复核安全网单测（用 fake LLM，不花真实 token）。

覆盖：
  1) 默认关 → 返回 []（零成本、零回归）；
  2) 开启 + LLM 回 OK → 返回 []（不误报）；
  3) 开启 + LLM 回「问题：...」→ 返回对应警示（多行/带编号/带「问题：」前缀 都能解析）；
  4) LLM 抛异常 → 降级返回 []（不影响正常回答）。
用法（容器内）：python3 tools/test_llm_review.py
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(_ROOT, "fuguoai_core")):
    _ROOT = os.environ.get("FUGUOAI_ROOT", "/opt/fuguoai-server")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _FakeLLM:
    """可注入返回内容；response_no_stream 返回字符串列表（逐 chunk）。"""
    def __init__(self, reply):
        self._reply = reply

    def response_no_stream(self, system_prompt=None, user_prompt=None):
        # 逐行作为 chunk，模拟流式；同时检验解析对多 chunk 拼接鲁棒
        for line in self._reply.split("\n"):
            yield line


def _make_kb(enabled_review):
    from fuguoai_config.config_loader import load_config
    from fuguoai_core.utils.tourism_kb import get_tourism_kb
    from fuguoai_core.utils.config_holder import set_config
    cfg = asyncio.run(load_config())
    set_config(cfg)
    kb = get_tourism_kb()
    if kb is None or not kb.enabled:
        print("SKIP: 文旅 KB 未启用")
        sys.exit(0)
    kb.faith_llm_review_enabled = enabled_review
    return kb


CASES = [
    # (名称, 是否开启, fake 回复, 期望 flags 数, 期望含子串)
    ("默认关", False, "OK", 0, ""),
    ("开启+OK", True, "OK", 0, ""),
    ("开启+空白", True, "   \n  ", 0, ""),
    ("开启+单一问题", True, "问题：回答称门票100元，但上下文未提供票价", 1, "门票100元"),
    ("开启+多行带编号", True, "问题：A市被写成B市。\n2、开放时间无据。\n3、OK", 2, "A市"),
    ("开启+无前缀", True, "回答中的里程与上下文不符", 1, "里程"),
]


def main():
    kb = _make_kb(False)  # 先造实例，再逐个覆盖开关
    passed = 0
    failed = 0
    for name, en, reply, exp_n, sub in CASES:
        kb.faith_llm_review_enabled = en
        fake = _FakeLLM(reply)
        kb._set_test_llm(fake)
        flags = kb._llm_faithfulness_review({}, "测试问题", "这是上下文", "这是回答")
        kb._set_test_llm(None)
        ok = (len(flags) == exp_n) and (sub == "" or any(sub in f for f in flags))
        tag = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"[{tag}] {name}: flags={len(flags)} 期望={exp_n}  {flags}")
    # 异常降级：开启但 LLM 抛错 → []
    kb.faith_llm_review_enabled = True
    class _Boom:
        def response_no_stream(self, system_prompt=None, user_prompt=None):
            raise RuntimeError("boom")
    kb._set_test_llm(_Boom())
    fl = kb._llm_faithfulness_review({}, "q", "ctx", "ans")
    kb._set_test_llm(None)
    ok = fl == []
    tag = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"[{tag}] 开启+LLM异常降级: flags={fl}")
    print(f"\nTOTAL={passed + failed} PASS={passed} FAIL={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
