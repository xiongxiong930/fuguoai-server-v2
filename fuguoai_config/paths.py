# -*- coding: utf-8 -*-
"""
资产路径单一真相源（自进化维度：根除路径漂移类静默失效）。

历史事故 (2026-08-01 B1)：
    代码用 CWD 相对字符串 ``"config/assets/..."`` 引用音频资产，重构后真实资产已挪到
    ``fuguoai_config/assets/``，但 4 个文件的 7 处引用仍写旧路径，导致绑定提示音 / 超长
    输出提示音 / TTS 通知音 / 唤醒问候音 运行时**全部 FileNotFoundError 静默失效**——
    不影响启动、不报明显错误，只有真机才暴露。

本模块把资产目录锚定到包位置（与进程 CWD 无关），并提供启动自检
``verify_critical_assets()``，把"静默失效"变成"启动即报错"。
"""
import logging
import os

logger = logging.getLogger("fuguoai.assets")

# 资产目录跟随 fuguoai_config 包位置，绝不依赖进程 CWD —— 从根上消除 B1 类漂移。
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# 关键音频资产：缺失会导致核心交互（绑定/超长/通知/唤醒）失效。启动自检逐项断言存在。
CRITICAL_ASSETS = [
    "bind_code.wav",
    "bind_not_found.wav",
    "max_output_size.wav",
    "tts_notify.mp3",
    "wakeup_words.wav",
    "wakeup_words_short.wav",
] + [f"bind_code/{i}.wav" for i in range(10)]


def verify_critical_assets(assets_dir: str = ASSETS_DIR) -> bool:
    """
    启动自检：断言关键音频资产齐全。

    - 资产目录整体缺失（如本地纯开发环境未拉取资源）：仅告警并跳过，不阻塞启动。
    - 目录存在但关键文件缺失（典型部署事故）：抛出 FileNotFoundError，让启动**大声失败**。

    返回 True 表示自检通过（或已跳过）。
    """
    if not os.path.isdir(assets_dir):
        # 本地开发环境可能不带音频资源，跳过而非阻塞。
        logger.warning("资产目录不存在，跳过关键资产自检: %s", assets_dir)
        return False
    missing = [a for a in CRITICAL_ASSETS if not os.path.exists(os.path.join(assets_dir, a))]
    if missing:
        raise FileNotFoundError(
            "关键音频资产缺失，服务无法提供完整交互，请检查 fuguoai_config/assets/: "
            + ", ".join(missing)
        )
    logger.info("关键音频资产自检通过 (%d 项): %s", len(CRITICAL_ASSETS), assets_dir)
    return True
