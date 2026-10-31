"""复果缓存：类型与策略定义。

每类缓存一条策略（CachePolicy）：
  ttl=None 表示不过期（只能手动失效）；lru=True 时超容按最久未用驱逐，
  否则按插入序驱逐。策略集中在 POLICIES，调用侧不感知。
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional


class CacheType(Enum):
    """缓存类型（每类一个独立空间，互不干扰）。"""

    LOCATION = "location"                    # IP → 城市（手动失效）
    WEATHER = "weather"                      # 城市 → 天气文案
    LUNAR = "lunar"                          # 日期 → 农历
    INTENT = "intent"                        # 意图识别结果
    IP_INFO = "ip_info"                      # IP 归属地原始应答
    CONFIG = "config"                        # 配置/模板（手动失效）
    DEVICE_PROMPT = "device_prompt"          # 设备级系统提示词（手动失效）
    VOICEPRINT_HEALTH = "voiceprint_health"  # 声纹服务健康探测
    AUDIO_DATA = "audio_data"                # 解码后的音频帧列表


@dataclass(frozen=True)
class CachePolicy:
    ttl: Optional[float] = 300      # 秒；None = 不过期
    max_size: int = 1000            # 超容触发驱逐
    lru: bool = False               # True = 最久未用驱逐；False = 先进先出
    sweep_interval: float = 60      # 过期条目周期清扫间隔（秒）


POLICIES = {
    CacheType.LOCATION: CachePolicy(ttl=None),
    CacheType.IP_INFO: CachePolicy(ttl=86400),                    # 24h
    CacheType.WEATHER: CachePolicy(ttl=28800),                    # 8h
    CacheType.LUNAR: CachePolicy(ttl=2592000, max_size=365),      # 30d
    CacheType.INTENT: CachePolicy(ttl=600, lru=True),             # 10min
    CacheType.CONFIG: CachePolicy(ttl=None, max_size=20),
    CacheType.DEVICE_PROMPT: CachePolicy(ttl=None),
    CacheType.VOICEPRINT_HEALTH: CachePolicy(ttl=600, max_size=100),
    CacheType.AUDIO_DATA: CachePolicy(ttl=600, max_size=100),
}


def policy_for(cache_type: CacheType) -> CachePolicy:
    return POLICIES.get(cache_type, CachePolicy())
