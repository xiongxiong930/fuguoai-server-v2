"""复果系统提示词管理。

两级提示词：
  快路径 get_quick_prompt   — 连接建立时立即可用（设备缓存版 > 传入原始版）。
  慢路径 build_enhanced_prompt — 拿到位置/天气/上下文后用 Jinja2 模板渲染增强版，
                                 渲染结果写回设备缓存，下次连接直接走快路径。
update_context_info 负责在慢路径前同步预热位置与动态上下文。

约定：
  - 模板文件默认 agent-base-prompt.txt（config.prompt_template 可覆盖），
    模板内容进 CONFIG 缓存（手动失效）。
  - {{current_time}} 保留为占位符不在此处渲染，由 dialogue 在每轮请求时替换，
    避免增强提示词因分钟跳动而失去缓存价值。
"""

import asyncio
import os
import threading
from typing import Any, Dict, TYPE_CHECKING

from jinja2 import Template

from fuguoai_config.logger import setup_logging
from fuguoai_core.utils.cache.manager import cache_manager, CacheType

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__

# 表情候选集：注入模板供 LLM 在回复中携带情绪标记
EMOJI_List = [
    "😶", "🙂", "😆", "😂", "😔", "😠", "😭",
    "😍", "😳", "😲", "😱", "🤔", "😉", "😎",
    "😌", "🤤", "😘", "😏", "😴", "😜", "🙄",
]

_DEFAULT_TEMPLATE_PATH = "agent-base-prompt.txt"
_WEATHER_TIMEOUT_S = 10


class PromptManager:
    """管理设备系统提示词的加载、增强与缓存。"""

    def __init__(self, config: Dict[str, Any], logger=None):
        self.config = config
        self.logger = logger or setup_logging()
        self.base_prompt_template = None
        self.context_data = {}

        from fuguoai_core.utils.context_provider import ContextDataProvider
        self.context_provider = ContextDataProvider(config, self.logger)

        self._load_base_template()

    # ------------------------------------------------------------
    # 模板加载
    # ------------------------------------------------------------

    def _load_base_template(self):
        """读取基础模板（CONFIG 缓存优先，文件兜底）。"""
        template_path = self.config.get("prompt_template") or _DEFAULT_TEMPLATE_PATH
        cache_key = f"prompt_template:{template_path}"
        try:
            cached = cache_manager.get(CacheType.CONFIG, cache_key)
            if cached is not None:
                self.base_prompt_template = cached
                return

            if not os.path.exists(template_path):
                self.logger.bind(tag=TAG).warning(f"未找到{template_path}文件")
                return

            with open(template_path, "r", encoding="utf-8") as f:
                content = f.read()
            cache_manager.set(CacheType.CONFIG, cache_key, content)
            self.base_prompt_template = content
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载提示词模板失败: {e}")

    def _template_uses(self, *names: str) -> bool:
        """模板中是否引用了任一变量名（决定要不要做昂贵的数据预热）。"""
        return bool(self.base_prompt_template) and any(
            n in self.base_prompt_template for n in names
        )

    # ------------------------------------------------------------
    # 快路径
    # ------------------------------------------------------------

    def get_quick_prompt(self, user_prompt: str, device_id: str = None) -> str:
        """立即返回可用的系统提示词：设备缓存的增强版优先，否则用原始版并预缓存。"""
        if device_id:
            cached = cache_manager.get(
                CacheType.DEVICE_PROMPT, f"device_prompt:{device_id}"
            )
            if cached is not None:
                return cached
            cache_manager.set(
                CacheType.DEVICE_PROMPT, f"device_prompt:{device_id}", user_prompt
            )
        self.logger.bind(tag=TAG).info(f"使用快速提示词: {user_prompt[:50]}...")
        return user_prompt

    # ------------------------------------------------------------
    # 上下文数据预热（慢路径前置）
    # ------------------------------------------------------------

    def _get_location_info(self, client_ip: str) -> str:
        """IP → 城市（LOCATION 缓存，手动失效）。"""
        try:
            cached = cache_manager.get(CacheType.LOCATION, client_ip)
            if cached is not None:
                return cached

            from fuguoai_core.utils.util import get_ip_info
            city = get_ip_info(client_ip, self.logger).get("city") or "未知位置"
            cache_manager.set(CacheType.LOCATION, client_ip, city)
            return city
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取位置信息失败: {e}")
            return "未知位置"

    def _weather_cache_key(self, location: str) -> str:
        """天气上下文缓存 key（含 provider，切换数据源后自动失效）。"""
        provider = (
            self.config.get("plugins", {}).get("get_weather", {}).get("provider") or "wttr"
        ).lower()
        return f"{provider}:{location}"

    def _get_weather_info(self, conn: "ConnectionHandler", location: str) -> str:
        """城市 → 天气文案（WEATHER 缓存）。

        在独立守护线程里跑同步网络请求（generate_weather_report 内部为同步 httpx），
        join 超时即返回，绝不阻塞事件循环。返回天气报告或空串（失败/超时）。
        """
        try:
            key = self._weather_cache_key(location)
            cached = cache_manager.get(CacheType.WEATHER, key)
            if cached is not None:
                return cached

            from fuguoai_plugins.functions.get_weather import generate_weather_report

            weather_config = self.config.get("plugins", {}).get("get_weather", {})
            result: dict = {}

            def _worker():
                try:
                    result["r"] = generate_weather_report(location, "zh_CN", weather_config)
                except Exception as e:  # noqa: BLE001
                    result["e"] = e

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join(timeout=_WEATHER_TIMEOUT_S)
            if t.is_alive():
                self.logger.bind(tag=TAG).warning("天气预热超时，跳过")
                return ""
            if "e" in result:
                self.logger.bind(tag=TAG).error(f"获取天气信息失败: {result['e']}")
                return ""
            report = result.get("r", "")
            cache_manager.set(CacheType.WEATHER, key, report)
            return report
        except Exception as e:  # noqa: BLE001
            self.logger.bind(tag=TAG).error(f"获取天气信息失败: {e}")
            return ""

    def update_context_info(self, conn, client_ip: str):
        """同步预热位置与设备动态上下文（仅当模板真的引用它们）。"""
        try:
            if client_ip and self._template_uses("local_address", "weather_info"):
                loc = self._get_location_info(client_ip)
                if loc and loc != "未知位置":
                    # 本地天气预置进上下文（混合形态：本地在上下文，外地才走工具）
                    self._get_weather_info(conn, loc)

            if getattr(conn, "device_id", None):
                self.context_data = (
                    self.context_provider.fetch_all(conn.device_id)
                    if self._template_uses("dynamic_context")
                    else ""
                )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"更新上下文信息失败: {e}")

    # ------------------------------------------------------------
    # 慢路径
    # ------------------------------------------------------------

    def build_enhanced_prompt(
        self, user_prompt: str, device_id: str, client_ip: str = None, *args, **kwargs
    ) -> str:
        """渲染增强提示词并写回设备缓存；任何异常都回退原始提示词。"""
        if not self.base_prompt_template:
            return user_prompt

        try:
            from fuguoai_core.utils.current_time import (
                get_current_date,
                get_current_weekday,
                get_current_lunar_date,
            )

            # 位置/天气仅取已预热的缓存，绝不在渲染路径上做网络请求
            local_address = ""
            weather_info = ""
            if client_ip:
                local_address = cache_manager.get(CacheType.LOCATION, client_ip) or ""
                if local_address:
                    weather_info = (
                        cache_manager.get(CacheType.WEATHER, self._weather_cache_key(local_address)) or ""
                    )

            selected_tts = self.config.get("selected_module", {}).get("TTS", "")
            language = (
                self.config.get("TTS", {}).get(selected_tts, {}).get("language")
                or "中文"
            )

            enhanced = Template(self.base_prompt_template).render(
                base_prompt=user_prompt,
                current_time="{{current_time}}",  # 留给 dialogue 每轮替换
                today_date=get_current_date(),
                today_weekday=get_current_weekday(),
                lunar_date=get_current_lunar_date() + "\n",
                local_address=local_address,
                weather_info=weather_info,
                emojiList=EMOJI_List,
                device_id=device_id,
                client_ip=client_ip,
                dynamic_context=self.context_data,
                language=language,
                *args,
                **kwargs,
            )

            cache_manager.set(
                CacheType.DEVICE_PROMPT, f"device_prompt:{device_id}", enhanced
            )
            self.logger.bind(tag=TAG).info(f"构建增强提示词成功，长度: {len(enhanced)}")
            return enhanced
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"构建增强提示词失败: {e}")
            return user_prompt
