"""模块装配中枢（复果自有实现）。

消费 tts/llm/intent/memory/vad/asr 六个工厂，按调用方传入的能力开关有选择地
组装进一个 dict；并负责 TTS 抖动兜底后端挂载与声纹(腾讯云说话人识别)配置就绪检查。
入口 ``initialize_modules`` 由 websocket_server 与 connection 在启动/重初始化时调用。
"""

from typing import Dict, Any

from fuguoai_config.logger import setup_logging
from fuguoai_core.utils import tts, llm, intent, memory, vad, asr

TAG = __name__
logger = setup_logging()

# 返回键(小写) -> 配置段(大写) 映射
_SECTION = {
    "tts": "TTS",
    "llm": "LLM",
    "intent": "Intent",
    "memory": "Memory",
    "vad": "VAD",
    "asr": "ASR",
}


def _resolve_type(config: Dict[str, Any], section: str, module_name: str) -> str:
    """从配置段取模块类型；未显式声明 type 时回退到模块名本身。"""
    return config[section][module_name].get("type", module_name)


def _started(label: str, picked: str) -> str:
    """装配完成日志文案（复果自有表述）。"""
    return f"组件装配完成: {label} <- {picked}"


def _delete_audio_flag(config: Dict[str, Any]) -> bool:
    """配置项 delete_audio 归一化为布尔（兼容 'true'/'1'/'yes' 文本与 True）。"""
    return str(config.get("delete_audio", True)).lower() in ("true", "1", "yes")


def initialize_modules(
    logger,
    config: Dict[str, Any],
    init_vad=False,
    init_asr=False,
    init_llm=False,
    init_tts=False,
    init_memory=False,
    init_intent=False,
) -> Dict[str, Any]:
    """按开关装配各 provider 工厂实例。

    :param logger: 调用方传入的日志器（遮蔽模块级日志器）
    :param config: 配置字典
    :returns: 键为模块名、值为已初始化实例的 dict
    """
    mods: Dict[str, Any] = {}

    # 标准工厂：(返回键, 开关, 工厂模块, 额外位置实参的 config 键)
    standard = [
        ("llm", init_llm, llm, ()),
        ("intent", init_intent, intent, ()),
        ("vad", init_vad, vad, ()),
        ("memory", init_memory, memory, ("summaryMemory",)),
    ]
    for label, enabled, factory, extras in standard:
        if not enabled:
            continue
        section = _SECTION[label]
        picked = config["selected_module"][section]
        positional = [
            _resolve_type(config, section, picked),
            config[section][picked],
        ]
        for extra_key in extras:
            positional.append(config.get(extra_key, None))
        mods[label] = factory.create_instance(*positional)
        logger.bind(tag=TAG).info(_started(label, picked))

    if init_tts:
        mods["tts"] = initialize_tts(config)
    if init_asr:
        mods["asr"] = initialize_asr(config)
    return mods


def initialize_tts(config):
    section = _SECTION["tts"]
    picked = config["selected_module"][section]
    instance = tts.create_instance(
        _resolve_type(config, section, picked),
        config[section][picked],
        _delete_audio_flag(config),
    )
    _mount_backup_tts(instance, config, picked)
    return instance


def _mount_backup_tts(primary, config, primary_name):
    """为主 TTS 挂载抖动兜底后端（默认 TencentTTS）。失败静默，不影响主用。"""
    backup = config.get("tts_fallback_module") or "TencentTTS"
    if primary_name == backup:
        return
    pool = config.get("TTS", {})
    if backup not in pool:
        logger.bind(tag=TAG).info(f"未配置兜底TTS({backup})，跳过挂载")
        return
    try:
        spec = pool[backup]
        backup_inst = tts.create_instance(
            spec.get("type", backup),
            spec,
            _delete_audio_flag(config),
        )
        primary._fallback_tts = backup_inst
        logger.bind(tag=TAG).info(f"已挂载 TTS 兜底后端: {backup}")
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"TTS 兜底后端挂载失败(已忽略): {exc}")


def initialize_asr(config):
    section = _SECTION["asr"]
    picked = config["selected_module"][section]
    instance = asr.create_instance(
        _resolve_type(config, section, picked),
        config[section][picked],
        _delete_audio_flag(config),
    )
    logger.bind(tag=TAG).info("ASR 模块装配完成")
    return instance


def initialize_voiceprint(asr_instance, config):
    """声纹(腾讯云说话人识别)配置就绪校验。

    真正的 provider 经 connection._initialize_voiceprint 单例接入；
    此处只做配置完整性检查，供启动日志/诊断使用（不再调用 asr 实例方法）。
    """
    vp = config.get("voiceprint") or {}
    if not vp or vp.get("provider") != "tencent":
        return False
    if not (vp.get("secret_id") and vp.get("secret_key") and vp.get("group_id")):
        logger.bind(tag=TAG).warning(
            "声纹识别(腾讯云)配置不完整：缺少 secret_id/secret_key/group_id"
        )
        return False
    logger.bind(tag=TAG).info("声纹识别(腾讯云)配置已就绪")
    return True
