"""占位 TTS 提供方（复果原生实现，clean-room 重写）。

当配置里没有可用 TTS 后端（腾讯云 / Edge / CosyVoice 等）时回落到本类。
它本身不接入任何合成服务，仅在被调用时记录错误，避免上层因缺失后端而静默崩。
文件名生成等基础能力保留，便于排查「为何没出声」。
"""

import os
import uuid

from fuguoai_config.logger import setup_logging
from fuguoai_core.providers.tts.base import TTSProviderBase

_log = setup_logging()
_EXT = ".wav"


class DefaultTTS(TTSProviderBase):
    """未配置有效 TTS 后端时的占位提供方。"""

    def __init__(self, config, delete_audio_file=True):
        super().__init__(config, delete_audio_file)
        self._cfg = config

    def _target_dir(self):
        """解析并保证输出目录存在，返回其绝对可用路径。"""
        d = self._cfg.get("output_dir") or "tmp"
        os.makedirs(d, exist_ok=True)
        return d

    def generate_filename(self):
        """生成全局唯一的音频文件名（输出目录下的 .wav）。"""
        return os.path.join(self._target_dir(), uuid.uuid4().hex + _EXT)

    async def text_to_speak(self, text, output_file):
        _log.error("TTS backend missing; verify tts section in config")
