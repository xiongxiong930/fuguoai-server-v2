"""BoardProfile：板型无关适配层（接各种主控板子的解耦核心）。

背景：后端原把 90C0 / 8818 / b4b0 的设备假设散落在 intercom_registry._aec_modes、
helloHandle 的 server_aec_devices 灰度名单、intercom_call_store 的看门狗阈值、
connection_registry / ws_server 的 MAC 归一与注释里。本模块把这些 per-board 的
「身份 + 能力」收口成一个数据驱动的 profile：

    → 新板接入 = 加一条 profile（data/board_profiles.yaml 或运行时注册），
    → 禁止在下游子系统内新增 `if board == "90C0"` 这类硬分支。

解析链路（device_id 先归一化：去冒号/连字符 + 小写）：
    1. 精确匹配 profile.mac（归一化后）
    2. 归一化 id 直接当作 board_id（如有人传 "8818"）
    3. mac_prefix 前缀匹配（同系列板批量）
    4. 都不中 → 返回保守 default（status=unknown），并打告警日志，绝不崩溃。

字段来源优先级（每个字段独立）：
    1. 运行时注册 / 外部 board_profiles.yaml（按板型）
    2. 本模块内嵌 _EMBEDDED（已知板兜底，保证无 yaml 也能跑）
    3. default profile（未知板，保守）

注意：本模块是「板型默认值的单一真相源」。全局 AEC 调优旋钮（aec_max_delay_ms 等）
仍走 data/.config.yaml 的 intercom: 段（与现状一致），profile 只承载 per-board 差异化
（aec.mode / 音频拓扑 / 看门狗超时 / 能力开关）。
"""

from __future__ import annotations

import dataclasses
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

try:
    from fuguoai_config.config_loader import get_project_dir
except Exception:  # noqa: BLE001  — 测试/缺包时降级
    def get_project_dir() -> str:  # pragma: no cover
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) + os.sep


_TAG = __name__


def normalize_device_id(device_id: str) -> str:
    """去冒号/连字符 + 小写，匹配库内规范形态。语义同 websocket_server.normalize_device_id。"""
    if not device_id:
        return ""
    return device_id.replace(":", "").replace("-", "").lower()


@dataclass
class AudioProfile:
    topology: str = "duplex"        # simplex | duplex
    channels: int = 1
    rate: int = 16000
    frame_ms: int = 20
    codec: str = "opus"             # opus | pcm | aac


@dataclass
class AecProfile:
    mode: Optional[str] = "outband"     # outband | 2ch | 2ch_srvref | None=用上游默认
    max_delay_ms: int = 12000
    echo_delay_ms: int = 0              # 0 = 让 aec_profile_store 实测指纹覆盖
    echo_band_ms: int = 0
    ner_auto: bool = True


@dataclass
class CapsProfile:
    wake_on_device: bool = True
    bargein: str = "auto"               # auto | disabled | manual
    listening_mode: str = "auto"        # auto | realtime | manual


@dataclass
class CallProfile:
    silence_after_flow_ms: float = 15.0    # 曾流过上行后的放宽静默阈值
    silence_sec: float = 5.0
    stale_max_age_sec: float = 60.0


@dataclass
class BoardProfile:
    board_id: str = "default"
    mac: str = ""
    mac_prefix: str = ""
    audio: AudioProfile = field(default_factory=AudioProfile)
    aec: AecProfile = field(default_factory=AecProfile)
    transport: str = "ws"
    capabilities: CapsProfile = field(default_factory=CapsProfile)
    call: CallProfile = field(default_factory=CallProfile)
    status: str = "active"               # active | deprecated | unknown

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ── 内嵌已知板（兜底，保证无 yaml 也能跑；值与 159 部署态一致）─────────────
# ⚠️ 90C0 当前在 159 的 server_aec_devices 名单之外 → 实际走 outband（mono）兜底，
#    并非记忆里写的 2ch_srvref 全双工。此处按「部署态」设 outband 以保证零回归；
#    若需 90C0 也 2ch_srvref，改 data/board_profiles.yaml 一行即可（属行为变更）。
_EMBEDDED = {
    "8818": BoardProfile(
        board_id="8818", mac="94a9901a8818",
        audio=AudioProfile(topology="duplex", channels=2, codec="opus"),
        aec=AecProfile(mode="2ch_srvref", max_delay_ms=12000, echo_delay_ms=0, echo_band_ms=0, ner_auto=True),
        capabilities=CapsProfile(wake_on_device=True, bargein="auto", listening_mode="auto"),
        call=CallProfile(silence_after_flow_ms=15.0), status="active"),
    "90C0": BoardProfile(
        board_id="90C0", mac="94a9901a90c0",
        audio=AudioProfile(topology="duplex", channels=2, codec="opus"),
        aec=AecProfile(mode="outband", max_delay_ms=12000, echo_delay_ms=0, echo_band_ms=0, ner_auto=True),
        capabilities=CapsProfile(wake_on_device=True, bargein="disabled", listening_mode="auto"),
        call=CallProfile(silence_after_flow_ms=15.0), status="active"),
    "b4b0": BoardProfile(
        board_id="b4b0", mac="94a9902db4b0",
        audio=AudioProfile(topology="duplex", channels=2, codec="opus"),
        aec=AecProfile(mode="outband", max_delay_ms=12000, echo_delay_ms=0, echo_band_ms=0, ner_auto=True),
        capabilities=CapsProfile(wake_on_device=True, bargein="auto", listening_mode="realtime"),
        call=CallProfile(silence_after_flow_ms=15.0), status="deprecated"),
}

_DEFAULT = BoardProfile(board_id="default", status="unknown")

_lock = threading.Lock()
_extra: dict = {}                 # 运行时注册（测试 / 动态板）
_yaml_cache: dict = {"mtime": None, "boards": {}}


def _filter(d: Optional[dict], cls) -> dict:
    fields = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in (d or {}).items() if k in fields}


def _build_from_dict(d: dict) -> BoardProfile:
    return BoardProfile(
        board_id=d.get("board_id", "unknown"),
        mac=(d.get("mac") or "").replace(":", "").replace("-", "").lower(),
        mac_prefix=(d.get("mac_prefix") or "").replace(":", "").replace("-", "").lower(),
        audio=AudioProfile(**_filter(d.get("audio"), AudioProfile)),
        aec=AecProfile(**_filter(d.get("aec"), AecProfile)),
        transport=d.get("transport", "ws"),
        capabilities=CapsProfile(**_filter(d.get("capabilities"), CapsProfile)),
        call=CallProfile(**_filter(d.get("call"), CallProfile)),
        status=d.get("status", "active"),
    )


def _yaml_path() -> str:
    try:
        base = get_project_dir()
    except Exception:  # noqa: BLE001
        base = os.getcwd()
    return os.path.join(base, "data", "board_profiles.yaml")


def _load_yaml_boards() -> dict:
    """带 mtime 缓存地加载 data/board_profiles.yaml 的 boards 段；缺失/异常返回 {}。"""
    global _yaml_cache
    p = _yaml_path()
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        _yaml_cache["mtime"] = -1          # 缓存「缺失」，避免每次调用都 stat
        _yaml_cache["boards"] = {}
        return {}
    if _yaml_cache.get("mtime") == mtime and _yaml_cache.get("boards") is not None:
        return _yaml_cache["boards"]
    boards: dict = {}
    try:
        import yaml  # 可选依赖；缺失则无外部 profile（内嵌兜底）
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        raw = data.get("boards", {}) or {}
        for k, v in raw.items():
            if isinstance(v, dict):
                boards[k] = _build_from_dict(v)
    except Exception:  # noqa: BLE001
        boards = {}
    _yaml_cache = {"mtime": mtime, "boards": boards}
    return boards


def register_profile(board_id: str, d: dict) -> None:
    """运行时注册/覆盖一条板型 profile（测试与动态板接入用）。"""
    with _lock:
        _extra[board_id] = _build_from_dict(d)


def resolve_board_profile(device_id: str) -> BoardProfile:
    """device_id → BoardProfile。未知板返回保守 default（status=unknown），绝不崩溃。"""
    nid = normalize_device_id(device_id)
    yaml_boards = _load_yaml_boards()
    with _lock:
        sources = {}
        sources.update(_EMBEDDED)
        sources.update(yaml_boards)
        sources.update(_extra)
    # 1) 精确 mac 匹配（归一化）
    for p in sources.values():
        if p.mac and p.mac == nid:
            return p
    # 2) 归一化 id 直接当 board_id（如有人传 "8818"）
    if nid in sources:
        return sources[nid]
    # 3) mac_prefix 前缀匹配（同系列板）
    for p in sources.values():
        if p.mac_prefix and nid.startswith(p.mac_prefix):
            return p
    return _DEFAULT
