"""M12 · 设备配置覆盖层（连接时拉取）（自研重构版）。

在 `core/connection.py::handle_connection` 中、`device_id` 就绪后、组件初始化前调用：
按 `device_id` 从 SQLite 读取设备智能体配置，按字段覆盖到 `self.config` 对应路径。

设计要点（与原实现一致，仅代码结构进化）
- **零热切**：仅连接建立时生效，不打断进行中的对话，无需 docker restart。
- **失败回落**：任何异常都原样返回 config，绝不中断连接（风险登记：T18 回归）。
- **量纲转换**：speed/volume 是 0.5–2.0 / 0.0–1.0 的语义值，由各 TTS provider 参数
  适配器转成对应 config 键（整数/小数），不预格式化成字符串。
- 与既有 `read_config_from_api` 推送式热更新互不冲突：本函数仅在连接建立时 merge 一次，
  若未来要「改完立即生效」，应复用既有 `update_config` 推送通道，而非在此另造。
"""
import logging

from fuguoai_core.utils.agent_config_store import get_agent_config_store

TAG = "agent_config_overlay"
logger = logging.getLogger(TAG)


def _tts_params(provider: str, speed, volume) -> dict:
    """按 provider 把语义 speed/volume 转成该 provider config 里的键。

    speed: 0.5–2.0（1.0=原速）；volume: 0.0–1.0（1.0=最大）。
    返回 dict，仅含被本次覆盖的键。
    """
    if provider == "EdgeTTS":
        out = {}
        if speed is not None:
            out["rate"] = int(round((speed - 1.0) * 100))      # -100..100（整数）
        if volume is not None:
            out["volume"] = int(round(volume * 100))           # 0..100（整数）
        return out
    if provider in ("AliTTS", "AliyunTTS", "Aliyun", "AliyunStream"):
        out = {}
        if speed is not None:
            out["speech_rate"] = int(round((speed - 1.0) * 500))  # -500..500
        if volume is not None:
            out["volume"] = int(round(volume * 100))              # 0..100
        return out
    if provider in ("TencentTTS", "TencentCloudTTS", "CosyVoice"):
        out = {}
        if speed is not None:
            out["speed"] = round(float(speed), 2)               # 0.5–2.0 透传
        if volume is not None:
            out["volume"] = round(volume * 10, 2)               # 0–10
        return out
    # 未知 provider：按 EdgeTTS 风格兜底（整数 rate/volume）
    out = {}
    if speed is not None:
        out["rate"] = int(round((speed - 1.0) * 100))
    if volume is not None:
        out["volume"] = int(round(volume * 100))
    return out


def _merge_prompt(config: dict, row: dict) -> None:
    """把 tone/dialect 风格与设备专属 prompt 拼接进 config["prompt"]。"""
    tone = row.get("tone")
    dialect = row.get("dialect")
    style_bits = []
    if tone:
        style_bits.append(f"用「{tone}」的语气说话")
    if dialect:
        style_bits.append(f"使用「{dialect}」交流")
    if style_bits:
        style_note = "\n\n# 表达风格\n" + "；".join(style_bits) + "。"
        base_prompt = row.get("prompt") or config.get("prompt") or ""
        config["prompt"] = base_prompt + style_note
    elif row.get("prompt"):
        config["prompt"] = row["prompt"]


def _merge_tts(config: dict, row: dict, provider: str) -> None:
    """切换 TTS 服务商并写入音色 / 语速 / 音量（量纲已转换）。"""
    if row.get("tts_provider") and isinstance(config.get("selected_module"), dict):
        config["selected_module"]["TTS"] = row["tts_provider"]
    if provider:
        tts_cfg = config.setdefault("TTS", {}).setdefault(provider, {})
        if row.get("voice"):
            tts_cfg["voice"] = row["voice"]
        tts_cfg.update(_tts_params(provider, row.get("speed"), row.get("volume")))


def _merge_wakeup(config: dict, row: dict) -> None:
    """把设备专属唤醒词并入 config，并按「唤醒词→问候语」映射设置 greeting_text。"""
    ww = row.get("wakeup_words")
    if not ww:
        return
    existing_ww = config.get("wakeup_words")
    ww_list = []
    if isinstance(existing_ww, str):
        ww_list = [existing_ww]
    elif isinstance(existing_ww, list):
        ww_list = list(existing_ww)
    if ww not in ww_list:
        ww_list.append(ww)
    if ww_list:
        config["wakeup_words"] = ww_list

    greetings = config.get("wake_word_greetings") or {}
    if isinstance(greetings, dict) and ww in greetings:
        # 取人格专属问候；无匹配则保留全局默认，绝不用唤醒词本身当问候语
        config["greeting_text"] = greetings[ww]


def apply_device_overlay(config: dict, device_id: str) -> dict:
    """把设备级配置 merge 进 config（原地修改并返回）。device_id 为空或查不到则原样返回。"""
    if not device_id or not isinstance(config, dict):
        return config
    try:
        row = get_agent_config_store().get_config(device_id)
        if not row:
            return config

        _merge_prompt(config, row)

        provider = row.get("tts_provider") or (config.get("selected_module") or {}).get("TTS")
        _merge_tts(config, row, provider)
        _merge_wakeup(config, row)

        logger.info(f"已应用设备配置覆盖层: device={device_id} provider={provider}")
        return config
    except Exception as e:
        # 绝不中断连接：回落全局配置
        logger.warning(f"设备配置覆盖失败，回落全局: device={device_id} err={e}")
        return config
