"""离线兜底（Offline Fallback）。

真进化动机：复果AI 在 LLM 主通道 + 备用通道**双双失败**时，`connection.py` 原逻辑
直接 `return None`，用户面对的是"死寂"——既没有 TTS 播报，也没有任何错误提示。
本模块把"该说什么"收敛为一个纯函数，使兜底行为：

1. 单一来源、可配置（优先级：offline_fallback.answer > system_error_response > 内置默认）；
2. 可在离线单元测试中断言（不依赖任何 LLM/网络），满足真进化条款的可度量验证要求。

绝不改变既有对话主流程；仅作为"最后一道 rescue"。
"""

from fuguoai_core.utils.util import get_system_error_response

_DEFAULT_OFFLINE = "我是复果，现在脑子有点转不动，稍后我们再聊～"


def build_offline_fallback(config: dict) -> str:
    """返回离线兜底文本。

    优先级：
      1. config["offline_fallback"]["answer"]（最具体，运营可随时改）
      2. config["system_error_response"]（既有系统异常话术，复用）
      3. 内置默认（保证永远有话说）
    """
    if not isinstance(config, dict):
        return _DEFAULT_OFFLINE
    cfg = config.get("offline_fallback", {}) or {}
    if isinstance(cfg, dict) and cfg.get("answer"):
        return str(cfg["answer"])
    # 回退到既有系统异常话术；若与内置默认等同（未配置）则退回内置默认
    sys_err = get_system_error_response(config)
    if sys_err and sys_err != _DEFAULT_OFFLINE:
        return sys_err
    return _DEFAULT_OFFLINE
