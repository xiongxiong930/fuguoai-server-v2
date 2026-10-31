"""离线兜底单测（无网络/无 LLM）。真进化验证：兜底文本来源优先级可断言。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils.offline_fallback import build_offline_fallback

_SYS_ERR_DEFAULT = "主人，复果现在有点忙，我们稍后再试吧。"


def test_override_priority():
    # offline_fallback.answer 优先级最高，覆盖系统异常话术
    cfg = {"offline_fallback": {"answer": "X"}, "system_error_response": "Y"}
    assert build_offline_fallback(cfg) == "X"


def test_empty_uses_system_error_default():
    # 未配置 offline_fallback 时，复用既有系统异常话术（含其默认值）
    assert build_offline_fallback({}) == _SYS_ERR_DEFAULT


def test_system_error_response_fallback():
    # 仅配置 system_error_response 时回退到它
    cfg = {"system_error_response": "稍后再试"}
    assert build_offline_fallback(cfg) == "稍后再试"


if __name__ == "__main__":
    test_override_priority()
    test_empty_uses_system_error_default()
    test_system_error_response_fallback()
    print("OFFLINE_FALLBACK_TESTS_PASS")
