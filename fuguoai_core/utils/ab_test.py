"""A/B 测试（按设备稳定分桶 + LLM 采样变体）。

真进化动机：复果AI 此前要"试一个新 prompt/新温度/新模型"只能全量上线、无法对照。
本模块提供**可控实验基础设施**：

  - 按 device_id 稳定哈希分桶（同一设备永远落同一桶，避免抖动导致体验反复横跳）；
  - 桶映射 LLM 采样覆盖（如温度），无需新建 LLM 实例即可产生可测量的回复差异；
  - 分桶计数与 judge 分数（见 dialogue_eval）按桶标签进入 METRICS，实验结论可直接对比；
  - 默认关闭；开启后仅对 B 桶注入覆盖，A 桶保持原样，对照成立。

设计：用轻量 `ABTestLLMWrapper` 包装 `self.llm`，在各生成接口注入覆盖参数，
不改动 LLM provider 内部、不引入新连接/配额。
"""

import threading

from fuguoai_config.logger import setup_logging
from fuguoai_core.utils.metrics import METRICS

TAG = __name__
logger = setup_logging()

_ab_instance = None
_ab_instance_lock = threading.Lock()


class ABTestLLMWrapper:
    """轻量包装：在底层 LLM 各生成接口注入覆盖参数（如 temperature）。"""

    def __init__(self, wrapped, overrides: dict):
        self._wrapped = wrapped
        self._overrides = dict(overrides or {})

    def _merge(self, kwargs):
        merged = dict(kwargs)
        merged.update(self._overrides)
        return merged

    def response(self, session_id, dialogue, device_id=None, device_session_id=None, **kwargs):
        yield from self._wrapped.response(
            session_id, dialogue, device_id=device_id, device_session_id=device_session_id, **self._merge(kwargs)
        )

    def response_with_functions(self, session_id, dialogue, functions=None, device_id=None, device_session_id=None, **kwargs):
        yield from self._wrapped.response_with_functions(
            session_id, dialogue, functions=functions, device_id=device_id, device_session_id=device_session_id, **self._merge(kwargs)
        )

    def response_no_stream(self, system_prompt, user_prompt, **kwargs):
        return self._wrapped.response_no_stream(system_prompt, user_prompt, **self._merge(kwargs))


class ABTest:
    def __init__(self, config):
        cfg = (config.get("ab_test", {}) or {}) if isinstance(config, dict) else {}
        self.enabled = bool(cfg.get("enabled", False))
        buckets = cfg.get("buckets") or {}
        self.buckets = {}
        for name, spec in buckets.items():
            if isinstance(spec, str):
                self.buckets[name] = {"block": spec, "overrides": {}}
            elif isinstance(spec, dict):
                self.buckets[name] = {
                    "block": spec.get("block"),
                    "overrides": spec.get("overrides", {}) or {},
                }
            else:
                self.buckets[name] = {"block": None, "overrides": {}}
        self._names = list(self.buckets.keys()) or ["A"]

    def bucket_for(self, device_id):
        """稳定分桶：相同 device_id 永远返回相同桶。"""
        if not device_id:
            return self._names[0]
        h = int(__import__("hashlib").md5(device_id.encode("utf-8")).hexdigest(), 16)
        return self._names[h % len(self._names)]

    def resolve(self, device_id):
        """返回 (bucket_name, overrides_dict)。"""
        if not self.enabled or not self.buckets:
            return (self._names[0], {})
        name = self.bucket_for(device_id)
        spec = self.buckets.get(name, {})
        return (name, spec.get("overrides", {}) or {})

    def wrap(self, device_id, llm):
        """按桶包装 self.llm；无覆盖则原样返回。每次分桶都计入 METRICS。"""
        if not self.enabled or not llm:
            return llm
        name, overrides = self.resolve(device_id)
        METRICS.inc(
            "fuguoai_ab_bucket_total",
            labels={"bucket": name},
            help_text="A/B 分桶分配计数",
        )
        if overrides:
            return ABTestLLMWrapper(llm, overrides)
        return llm


def get_ab_test(config):
    """进程级单例；支持运行时热更新 enabled 开关。"""
    global _ab_instance
    with _ab_instance_lock:
        if _ab_instance is None:
            _ab_instance = ABTest(config)
        else:
            _ab_instance.enabled = bool(
                ((config.get("ab_test", {}) or {}) if isinstance(config, dict) else {}).get("enabled", False)
            )
        return _ab_instance
