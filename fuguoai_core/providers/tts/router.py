"""TTS 路由器（dialect 感知，无状态委托）。

设计要点（与现有 ConnectionHandler 每连接一个 tts 实例的模型一致）：
- 每连接一个路由器实例；dialect 在 open_audio_channels 时按 device_id 从设备配置库
  解析一次并缓存（self._dialect），不存于会被并发覆盖的实例状态，线程安全无串台。
- to_tts_stream 仅做「选后端 → 委托子 provider 完整 to_tts_stream」，不复制 base 的
  分段/熔断/兜底逻辑；子 provider 各自持有完整状态(opus_encoder/queue/conn)。
- 方言后端(CosyVoice)不可达时，由 base 的 _fallback_tts 机制自动降级到腾讯云
  （open_audio_channels 中把 cosyvoice._fallback_tts 指向 tencent）。
- 提醒/非对话路径(text_to_speak)固定走腾讯云（无 dialect 上下文），并挂 tencent 作为
  兜底，保证联网提醒/播报永远有声、不再静音。

配置（data/.config.yaml，bind mount，由用户侧落地）：
  selected_module:
    TTS: router
  tts_fallback_module: none            # 路由层自管兜底，避免 modules_initialize 重复建实例
  TTS:
    TencentTTS: { ... 现有配置 ... }    # 普通话主用 + 兜底安全网
    CosyVoice:                         # 工作室本机
      base_url: http://<studio-ip>:10096
      timeout_ms: 8000
      dialect_speakers: { "武汉": spk_x, "宜昌": spk_y }
    router:
      default: tencent                 # 默认后端（普通话/未配置方言）
      routes:                          # 方言 → 后端
        "武汉": cosyvoice
        "宜昌": cosyvoice
        wuhan: cosyvoice
        yichang: cosyvoice
"""
import asyncio
import logging

from fuguoai_core.providers.tts.base import TTSProviderBase
from fuguoai_core.utils import tts as tts_factory

logger = logging.getLogger(__name__)


def _load_full_config():
    """取运行时完整 config（含 TTS.TencentTTS / TTS.CosyVoice）。"""
    try:
        from fuguoai_core.utils.cache.manager import cache_manager, CacheType

        return cache_manager.get(CacheType.CONFIG, "main_config")
    except Exception:
        return None


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        # config 此处为 TTS.router 子段；用其 default/routes，子后端配置从完整 config 取
        super().__init__(config, delete_audio_file)
        self._section = config or {}
        self.default_backend = self._section.get("default", "tencent")
        self.routes = self._section.get("routes") or {}
        self.tencent = None
        self.cosyvoice = None
        self.backends = {}
        self._dialect = None
        self._backends_built = False
        # 提醒路径兜底（open_audio_channels 中挂 tencent）；启动时置 None 由基类兼容
        self._fallback_tts = None

    # ------------------------------------------------------------------
    def _ensure_backends(self):
        """惰性构建子后端（运行时 config 一定已就绪，避免缓存时序问题）。"""
        if self._backends_built:
            return
        full = _load_full_config() or {}
        tts_section = full.get("TTS", {}) if isinstance(full, dict) else {}
        da = self.delete_audio_file

        tc_cfg = tts_section.get("TencentTTS") or self._section.get("tencent") or {}
        try:
            self.tencent = (
                tts_factory.create_instance("tencent", tc_cfg, da) if tc_cfg else None
            )
        except Exception as e:
            logger.error(f"路由器构建腾讯后端失败: {e}")
            self.tencent = None

        cv_cfg = tts_section.get("CosyVoice") or self._section.get("cosyvoice")
        if cv_cfg:
            try:
                self.cosyvoice = tts_factory.create_instance("cosyvoice", cv_cfg, da)
            except Exception as e:
                logger.error(f"路由器构建 CosyVoice 后端失败: {e}")
                self.cosyvoice = None

        self.backends = {"tencent": self.tencent}
        if self.cosyvoice is not None:
            self.backends["cosyvoice"] = self.cosyvoice
        self._backends_built = True

    def _resolve_dialect(self, conn):
        device_id = getattr(conn, "device_id", None)
        if not device_id:
            return None
        try:
            from fuguoai_core.utils.agent_config_store import get_agent_config_store

            row = get_agent_config_store().get_config(device_id)
            if row:
                return row.get("dialect")
        except Exception as e:
            logger.warning(f"路由器解析方言失败(回落默认): {e}")
        return None

    def _select_backend(self):
        dialect = getattr(self, "_dialect", None)
        name = self.routes.get(dialect) if dialect else None
        if not name or name not in self.backends:
            name = self.default_backend
        return (
            self.backends.get(name)
            or self.backends.get(self.default_backend)
            or self.tencent
        )

    # ------------------------------------------------------------------
    async def open_audio_channels(self, conn):
        self._ensure_backends()
        self.conn = conn
        self._dialect = self._resolve_dialect(conn)
        # 路由器自身：建 opus_encoder + 起 worker 线程（驱动 router.to_tts_stream）
        await super().open_audio_channels(conn)
        # 子后端绑定同一 conn + 各自 opus_encoder + 线程
        for b in self.backends.values():
            if b is not None:
                try:
                    await b.open_audio_channels(conn)
                except Exception as e:
                    logger.error(f"路由器子后端 open_audio_channels 失败: {e}")
        # 方言后端不可达 → 自动降级腾讯云（base 的 _fallback_tts 机制）
        if self.cosyvoice is not None and self.tencent is not None:
            self.cosyvoice._fallback_tts = self.tencent
        # 提醒路径兜底：router 自身 _fallback_synth_bytes 落到 tencent
        if self.tencent is not None:
            self._fallback_tts = self.tencent
        logger.info(
            f"TTS 路由器就绪: dialect={self._dialect} default={self.default_backend} "
            f"backends={[k for k, v in self.backends.items() if v]}"
        )

    # ------------------------------------------------------------------
    def to_tts_stream(self, text, opus_handler=None):
        """聊天路径：选后端 → 委托子 provider 完整 to_tts_stream（含其分段/熔断/兜底）。"""
        self._ensure_backends()
        backend = self._select_backend()
        if backend is None:
            logger.error("TTS 路由器无可用后端")
            if self.conn is not None:
                self._release_speaking_on_failure(self.conn)
            return None
        # 把当前方言透传给后端：方言后端据此选对说话人（武汉/宜昌不同 spk）。
        # 后端为每连接私有、调用同步顺序，逐句覆盖无并发串台风险。
        backend._dialect = self._dialect
        return backend.to_tts_stream(text, opus_handler)

    async def text_to_speak(self, text, output_file):
        """提醒/播报路径：固定走腾讯云（无 dialect 上下文），保证永远有声。"""
        self._ensure_backends()
        if self.tencent is None:
            raise RuntimeError("TTS 路由器: 腾讯后端不可用")
        return await self.tencent.text_to_speak(text, output_file)
