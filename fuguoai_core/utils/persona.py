"""设备人格（persona）——创新维度②E2。

真进化动机：复果AI 此前所有设备共用同一套 prompt 人设，"爷爷的复果"和
"孩子的复果"说话方式完全一样，零个性化。本模块提供按 device_id 的人格画像：

  - 三层合并：config.persona.default ← config.persona.devices[device_id]
    ← data/.persona.yaml[device_id]（文件层支持运行时修改，无需重启/改配置）
  - render(device_id) 产出 <persona> 中文块，随记忆通道注入动态 system；
  - set_persona() 原子持久化，为后续小程序人格管理 API 预留数据层；
  - 纯内存/带 mtime 缓存的文件读，全异常吞掉，绝不阻塞主对话。

画像字段（全部可选）：
  name        自称（默认"复果"）
  style       语气风格（如"温暖、简洁、自然口语"）
  traits      性格特点列表（如 ["幽默", "有耐心"]）
  catchphrase 口头禅
  taboo       禁忌列表（绝不主动提及的话题）
"""

import os
import threading
import time

import yaml

from fuguoai_config.logger import setup_logging
from fuguoai_config.config_loader import get_project_dir
from fuguoai_core.utils.config_holder import get_config as _get_config_holder
from fuguoai_core.utils.metrics import METRICS

TAG = __name__
logger = setup_logging()

_FIELDS = ("name", "style", "traits", "catchphrase", "taboo")

_instance = None
_instance_lock = threading.Lock()


class PersonaStore:
    """按 device_id 的人格画像存取与渲染。进程级单例，线程安全。"""

    # 文件层 mtime 缓存刷新间隔（秒）：避免每轮对话都 stat 文件
    _FILE_CHECK_INTERVAL = 5.0

    def __init__(self, config=None):
        conf = (config or {}).get("persona", {}) or {}
        self.enabled = bool(conf.get("enabled", False))
        self._default = self._clean(conf.get("default") or {})
        self._devices = {
            str(k): self._clean(v)
            for k, v in (conf.get("devices") or {}).items()
            if isinstance(v, dict)
        }
        self._path = os.path.join(get_project_dir(), "data", ".persona.yaml")
        self._lock = threading.Lock()
        self._file_cache = {}
        self._file_mtime = None
        self._last_check = 0.0

    # ---- 内部 ----
    @staticmethod
    def _clean(d):
        if not isinstance(d, dict):
            return {}
        return {k: d[k] for k in _FIELDS if k in d and d[k] not in (None, "", [])}

    def _load_file(self):
        """带 mtime 缓存读 data/.persona.yaml；文件不存在/损坏时降级为空。"""
        now = time.time()
        if now - self._last_check < self._FILE_CHECK_INTERVAL:
            return self._file_cache
        self._last_check = now
        try:
            if not os.path.exists(self._path):
                self._file_cache, self._file_mtime = {}, None
                return self._file_cache
            mtime = os.path.getmtime(self._path)
            if mtime == self._file_mtime:
                return self._file_cache
            with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                data = {}
            self._file_cache = {
                str(k): self._clean(v) for k, v in data.items() if isinstance(v, dict)
            }
            self._file_mtime = mtime
        except Exception as e:
            logger.bind(tag=TAG).warning(f"persona 文件读取失败(降级为空): {e}")
            self._file_cache = {}
        return self._file_cache

    # ---- 对外 ----
    def get_persona(self, device_id):
        """三层合并后的画像 dict。任何异常返回默认层，绝不抛出。"""
        try:
            merged = dict(self._default)
            did = str(device_id or "")
            merged.update(self._devices.get(did, {}))
            merged.update(self._load_file().get(did, {}))
            return merged
        except Exception:
            return dict(self._default)

    def render(self, device_id):
        """渲染 <persona> 中文块；未启用/画像为空返回空串。"""
        if not self.enabled:
            return ""
        try:
            p = self.get_persona(device_id)
            if not p:
                return ""
            lines = []
            if p.get("name"):
                lines.append(f"你的名字是「{p['name']}」，请以此自称。")
            if p.get("style"):
                lines.append(f"说话风格：{p['style']}。")
            traits = p.get("traits")
            if traits:
                if isinstance(traits, (list, tuple)):
                    traits = "、".join(str(t) for t in traits)
                lines.append(f"性格特点：{traits}。")
            if p.get("catchphrase"):
                lines.append(f"你偶尔会说口头禅：「{p['catchphrase']}」（不要每句都用）。")
            taboo = p.get("taboo")
            if taboo:
                if isinstance(taboo, (list, tuple)):
                    taboo = "、".join(str(t) for t in taboo)
                lines.append(f"绝不主动提及以下话题：{taboo}。")
            if not lines:
                return ""
            if METRICS:
                try:
                    METRICS.inc(
                        "fuguoai_persona_injections_total",
                        labels={"device_id": str(device_id or "unknown")},
                        help_text="人格画像注入对话的次数(按设备)",
                    )
                except Exception:
                    pass
            return "<persona>\n" + "\n".join(lines) + "\n</persona>"
        except Exception as e:
            logger.bind(tag=TAG).warning(f"persona 渲染失败(降级为空): {e}")
            return ""

    def set_persona(self, device_id, profile):
        """写入文件层（原子写），供后续管理 API 使用。返回合并后画像。"""
        did = str(device_id or "")
        if not did:
            raise ValueError("device_id 不能为空")
        cleaned = self._clean(profile or {})
        with self._lock:
            try:
                data = {}
                if os.path.exists(self._path):
                    with open(self._path, "r", encoding="utf-8", errors="replace") as f:
                        data = yaml.safe_load(f) or {}
                if not isinstance(data, dict):
                    data = {}
                if cleaned:
                    data[did] = cleaned
                else:
                    data.pop(did, None)
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                tmp = self._path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
                os.replace(tmp, self._path)
                # 失效缓存，下次读取即见新值
                self._file_mtime = None
                self._last_check = 0.0
            except Exception as e:
                logger.bind(tag=TAG).error(f"persona 持久化失败: {e}")
                raise
        return self.get_persona(did)


    def get_file_layer(self, device_id):
        """仅返回文件层（data/.persona.yaml）该设备的原始画像，未合并默认/配置层。

        供管理 API 做部分更新（先读旧值再合并），避免一次 PUT 把未传字段清空。
        """
        try:
            return dict(self._load_file().get(str(device_id or ""), {}))
        except Exception:
            return {}

    def update_persona(self, device_id, patch):
        """部分更新文件层画像（管理 API 友好）。

        - patch 只认 _FIELDS；未知字段静默忽略；
        - 显式 null 表示清除该字段（回到默认/配置层）；
        - 字符串字段拒绝非字符串；列表字段接受数组或逗号分隔字符串（自动拆分成数组）；
        返回更新后的三层合并画像。
        """
        did = str(device_id or "")
        if not did:
            raise ValueError("device_id 不能为空")
        if not isinstance(patch, dict):
            raise ValueError("patch 须为对象")
        patch = dict(patch)
        ok, err = _validate_patch(patch)
        if not ok:
            raise ValueError(err)
        current = self.get_file_layer(did)
        for k, v in patch.items():
            if k not in _FIELDS:
                continue
            if v is None:
                current.pop(k, None)
            else:
                current[k] = v
        return self.set_persona(did, current)


def _validate_patch(patch):
    """校验并就地规整 patch：列表字段逗号字符串拆成数组。返回 (ok, err_msg)。"""
    str_fields = ("name", "style", "catchphrase")
    list_fields = ("traits", "taboo")
    for k, v in patch.items():
        if v is None:
            continue
        if k in str_fields and not isinstance(v, str):
            return False, f"字段 {k} 须为字符串"
        if k in list_fields:
            if isinstance(v, str):
                patch[k] = [s.strip() for s in v.split(",") if s.strip()]
            elif not isinstance(v, list):
                return False, f"字段 {k} 须为数组或逗号分隔字符串"
    return True, ""


def get_persona_store(config=None):
    """进程级单例。首次调用以传入 config 初始化。"""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = PersonaStore(config or _get_config_holder())
    return _instance
