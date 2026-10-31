# -*- coding: utf-8 -*-
"""对讲 AEC 回声路径指纹自动建档（自进化方向①）。

设计意图
--------
`intercom_aec.IntercomAec.diag_summary()` 已能吐出每个设备的真实声学回声特征：
  - strong_delay_ms(min/max/mean/median/stdev)  → 真实回声延迟带
  - corr.strong_count / weak_count              → 回声是否线性可消
  - ref_active_rate                             → 环路抑制门控是否 fire

旧做法：90C0 的 `echo_delay_ms`(5~9s) / `band` / `echo_gain_cap` 全靠**人工凭记忆猜**
写死在 config。本模块把"诊断→参数"做成**后端自动闭环**：

  1. 每设备首通（或诊断窗口内）采集 diag_summary → 算出指纹档案；
  2. 落 `$FUGUOAI_DATA_DIR/aec_profile_<device>.json`；
  3. 后续新建 IntercomAec 实例时经 `load_profile()` 自动加载，覆盖实例默认参数。

闭环价值
--------
- 新设备零配置自动适配真实回声延迟，无需人工配 90C0 长延迟；
- 回声路径漂移（固件升级/换板）后下一通自动重算，自我修正；
- **纯离线建档 + 加载，不改 AEC 任何计算路径**，零回归风险（P0 安全）。

安全边界
--------
- 档案只在 `diag_summary()` 样本充足(>=MIN_SAMPLES)且强相关占比过半时才落盘，
  避免首帧噪声伪峰污染档案；
- 算出的 delay/band/gain 都经 **物理合理性钳制**（delay 非负且有上限、gain∈[0.1,0.95]），
  档案异常时回退代码默认，不拿脏数据驱动 AEC；
- 档案读取失败/格式错一律回退默认，绝不抛异常影响通话建立。
"""

import os
import json
import time
import threading
import logging

_logger = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("FUGUOAI_DATA_DIR", "/opt/fuguoai-server/data")
_PROFILE_SUBDIR = "aec_profiles"

# 建档最低样本数：低于此数 diag_summary 统计不可信，不落盘。
_MIN_SAMPLES = 200
# 强相关样本需过半（回声是 ref 的线性副本才可消），否则判定非线性/硬件问题，不建档。
_MIN_STRONG_RATIO = 0.5
# 延迟带物理合理性钳制（ms）
_DELAY_CAP_MS = 12000          # 实测 90C0 上限 ~9s，留余量
_BAND_MIN_MS = 500
_BAND_MAX_MS = 6000
# 增益物理合理性钳制（回声路径耦合增益通常 0.3~0.7）
_GAIN_MIN = 0.1
_GAIN_MAX = 0.95
# 档案最大静默期：超过此秒数视为回声路径可能漂移，下一通重新采集覆盖。
_PROFILE_STALE_SEC = 30 * 24 * 3600  # 30 天


def _profile_dir() -> str:
    d = os.path.join(_DATA_DIR, _PROFILE_SUBDIR)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _profile_path(device_id: str) -> str:
    # device_id 可能含冒号/MAC，文件名安全化
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in device_id)
    return os.path.join(_profile_dir(), f"aec_profile_{safe}.json")


def compute_profile(diag: dict) -> dict | None:
    """从 diag_summary() 输出算回声路径指纹；不可靠返回 None。

    返回 dict（可直接 json 落盘）：
      {echo_delay_ms, echo_delay_band_ms, echo_gain_cap,
       ref_active_rate, samples, corr_mean, source="auto", ts}
    """
    if not isinstance(diag, dict) or not diag.get("diag"):
        return None
    samples = diag.get("samples", 0)
    if samples < _MIN_SAMPLES:
        return None
    sd = diag.get("strong_delay_ms")
    if not sd:
        # 无强相关样本 → 回声非线性/不可消，不建档（交给 NER 兜底）
        return None
    corr = diag.get("corr", {})
    strong = corr.get("strong_count", 0)
    weak = corr.get("weak_count", 0)
    if (strong + weak) == 0 or strong / (strong + weak) < _MIN_STRONG_RATIO:
        return None

    # 延迟带：以 strong 样本 median 为锚，band = max(stdev×3, 最小带) 覆盖抖动
    median = sd.get("median", 0.0)
    stdev = sd.get("stdev", 0.0)
    delay = max(0.0, min(float(median), _DELAY_CAP_MS))
    band = max(_BAND_MIN_MS, min(stdev * 3.0, _BAND_MAX_MS))
    # 增益：corr_mean 越高回声越像 ref（强耦合）→ 增益钳上限；
    # 这里给一个保守默认（实测耦合 0.3~0.7），用 corr 微调：相关越高允许越接近上限。
    corr_mean = float(corr.get("mean", 0.0))
    gain = 0.6 + 0.3 * max(0.0, min(1.0, corr_mean))  # 0.6~0.9
    gain = max(_GAIN_MIN, min(gain, _GAIN_MAX))
    return {
        "echo_delay_ms": int(round(delay)),
        "echo_delay_band_ms": int(round(band)),
        "echo_gain_cap": round(gain, 3),
        "ref_active_rate": diag.get("ref_active_rate"),
        "samples": samples,
        "corr_mean": corr_mean,
        "source": "auto",
        "ts": int(time.time()),
    }


def save_profile(device_id: str, profile: dict) -> bool:
    """落盘指纹档案；成功返回 True。"""
    try:
        path = _profile_path(device_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子替换，避免半写档案
        return True
    except Exception as e:
        _logger.warning(f"[aec_profile] 落盘失败 device={device_id} err={e}")
        return False


def load_profile(device_id: str) -> dict | None:
    """读取指纹档案；不存在/过期/损坏返回 None（调用方回退默认）。"""
    try:
        path = _profile_path(device_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            p = json.load(f)
        if not isinstance(p, dict) or "echo_delay_ms" not in p:
            return None
        ts = p.get("ts", 0)
        if ts and (time.time() - ts) > _PROFILE_STALE_SEC:
            return None  # 过期：下一通重新采集
        return p
    except Exception:
        return None


def apply_profile(aec_instance, profile: dict) -> bool:
    """把档案参数覆盖进 IntercomAec 实例（仅改公开可调属性，不动计算状态）。

    返回 True 表示确实应用了自动档案（便于日志/metrics 区分'自动'vs'人工config'）。
    """
    if not profile or not aec_instance:
        return False
    try:
        aec_instance._echo_delay_ms = int(profile["echo_delay_ms"])
        aec_instance._echo_delay_band_ms = int(profile["echo_delay_band_ms"])
        if profile.get("echo_gain_cap") is not None:
            aec_instance._echo_gain_cap = float(profile["echo_gain_cap"])
        return True
    except Exception as e:
        _logger.warning(f"[aec_profile] 应用失败 err={e}")
        return False


class AecProfileStore:
    """线程安全的指纹建档协调器（单例）。

    用法（在 intercom_registry 建 IntercomAec 处）：
      store = get_aec_profile_store()
      prof = store.maybe_load(device_id)        # 建实例前拿档案
      aec = IntercomAec(..., echo_delay_ms=prof.get("echo_delay_ms",0), ...)
      store.maybe_apply(aec, prof)               # 覆盖实例
      ...
      # 通话结束时（diag 采集足够）
      diag = aec.diag_summary()
      store.maybe_record(device_id, diag)        # 自动建档
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict[str, list] = {}  # device_id -> 累计 diag 列表（跨帧聚合用）

    def maybe_load(self, device_id: str) -> dict | None:
        return load_profile(device_id)

    def maybe_apply(self, aec_instance, profile: dict | None) -> bool:
        if not profile:
            return False
        return apply_profile(aec_instance, profile)

    def maybe_record(self, device_id: str, diag: dict) -> dict | None:
        """通话结束时调用：算档案并落盘；返回落盘的 profile（None=未达建档条件）。"""
        prof = compute_profile(diag)
        if prof is None:
            return None
        if save_profile(device_id, prof):
            _logger.info(
                f"[aec_profile] 自动建档 device={device_id} "
                f"delay={prof['echo_delay_ms']}ms band={prof['echo_delay_band_ms']}ms "
                f"gain={prof['echo_gain_cap']} samples={prof['samples']}"
            )
            return prof
        return None


_store: AecProfileStore | None = None
_store_lock = threading.Lock()


def get_aec_profile_store() -> AecProfileStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = AecProfileStore()
    return _store
