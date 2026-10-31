"""轻量可观测性指标模块：零外部依赖、线程安全，直接渲染 Prometheus 文本格式。

- **零依赖**：仅使用标准库 ``threading`` 与 ``time``，不引入 prometheus_client，
  也不依赖本仓任何模块。
- **线程安全**：所有共享状态的读写均在可重入锁内完成，支持同一线程嵌套调用。
- **渲染**：``render()`` 输出 Prometheus 文本格式，供 ``/metrics`` 直接抓取，
  ``/healthz`` 亦可读 ``snapshot_*`` 与 ``uptime_seconds``。
- **三类指标**：
  * **Counter**（``inc``）：只增计数；
  * **Gauge**（``set_gauge`` / ``inc_gauge`` / ``dec_gauge``）：可直接覆盖，也可增可减；
  * **Histogram**（``observe``）：固定 bucket 的延迟分布，输出 ``_bucket`` / ``_sum`` / ``_count``。
- **全局单例 + 可重入锁**：模块级 ``METRICS`` 供各处直接上报，锁为 ``threading.RLock``。
"""

import threading
import time

# 延迟分布（秒）的固定 bucket 上界，升序排列。
_LATENCY_BUCKETS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 8.0, float("inf"))


def _labels_key(labels):
    """把标签字典归一化为可哈希且稳定的键。

    ``None`` 或空字典返回空元组；否则键与值统一转 ``str`` 后按 ``(k, v)`` 排序，
    保证同一组标签（无论书写顺序如何）得到同一个键。
    """
    if not labels:
        return tuple()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape(v):
    """按 Prometheus 文本格式转义标签值：反斜杠、双引号、换行。"""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _render_labels(key):
    """渲染标签片段，形如 ``{k="v",k2="v2"}``；空标签返回空串。"""
    if not key:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in key) + "}"


def _fmt(v):
    """渲染数值：整数值的 float 去掉小数点（``1.0`` -> ``1``），其余原样。"""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class _Metrics:
    """线程安全的指标注册表，支持 counter / gauge / histogram 三类指标。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._start_ts = time.time()
        self._counters = {}    # name -> {labels_key: value}
        self._gauges = {}      # name -> {labels_key: value}
        self._histograms = {}  # name -> {labels_key: {"buckets": [...], "sum": x, "count": n}}
        self._help = {}        # name -> help 文本

    # ------------------------------------------------------------------ 写入

    def inc(self, name, value=1.0, labels=None, help_text=None):
        """Counter 自增，默认 +1。"""
        key = _labels_key(labels)
        with self._lock:
            if help_text and name not in self._help:
                self._help[name] = help_text
            self._counters.setdefault(name, {})
            self._counters[name][key] = self._counters[name].get(key, 0.0) + value

    def set_gauge(self, name, value, labels=None, help_text=None):
        """Gauge 直接赋值（覆盖，非累加）。"""
        key = _labels_key(labels)
        with self._lock:
            if help_text and name not in self._help:
                self._help[name] = help_text
            self._gauges.setdefault(name, {})
            self._gauges[name][key] = float(value)

    def inc_gauge(self, name, value=1.0, labels=None, help_text=None):
        """Gauge 自增，默认 +1。"""
        key = _labels_key(labels)
        with self._lock:
            if help_text and name not in self._help:
                self._help[name] = help_text
            self._gauges.setdefault(name, {})
            self._gauges[name][key] = self._gauges[name].get(key, 0.0) + value

    def dec_gauge(self, name, value=1.0, labels=None):
        """Gauge 自减，等价于 ``inc_gauge(name, -value, labels)``。"""
        self.inc_gauge(name, -value, labels)

    def declare_counter(self, name, help_text):
        """预声明 counter：无数据时也出现在 ``/metrics`` 中；无条件覆盖 help。"""
        with self._lock:
            self._help[name] = help_text
            self._counters.setdefault(name, {})

    def declare_gauge(self, name, help_text):
        """预声明 gauge：无数据时也出现在 ``/metrics`` 中；无条件覆盖 help。"""
        with self._lock:
            self._help[name] = help_text
            self._gauges.setdefault(name, {})

    def observe(self, name, value, labels=None, help_text=None):
        """记录一次观测值到固定 bucket 的 histogram。

        bucket 上界升序，命中所有 ``value <= ub`` 的 bucket，天然形成累积计数。
        """
        key = _labels_key(labels)
        with self._lock:
            if help_text and name not in self._help:
                self._help[name] = help_text
            self._histograms.setdefault(name, {})
            h = self._histograms[name].get(key)
            if h is None:
                h = {"buckets": [0] * len(_LATENCY_BUCKETS), "sum": 0.0, "count": 0}
                self._histograms[name][key] = h
            for i, ub in enumerate(_LATENCY_BUCKETS):
                if value <= ub:
                    h["buckets"][i] += 1
            h["sum"] += value
            h["count"] += 1

    # ------------------------------------------------------------------ 读取

    def snapshot_counters(self):
        """返回 counters 的浅拷贝副本，改动返回值不影响内部状态。"""
        with self._lock:
            return {name: dict(per_label) for name, per_label in self._counters.items()}

    def snapshot_gauges(self):
        """返回 gauges 的浅拷贝副本，改动返回值不影响内部状态。"""
        with self._lock:
            return {name: dict(per_label) for name, per_label in self._gauges.items()}

    def snapshot_histograms(self):
        """返回 histograms 的拷贝：外层标签键转为 ``str``，buckets 转为 list。"""
        with self._lock:
            return {
                name: {
                    str(k): {
                        "buckets": list(h["buckets"]),
                        "sum": h["sum"],
                        "count": h["count"],
                    }
                    for k, h in per_label.items()
                }
                for name, per_label in self._histograms.items()
            }

    def uptime_seconds(self):
        """服务运行时长（秒），不持锁。"""
        return time.time() - self._start_ts

    # ------------------------------------------------------------------ 渲染

    def render(self):
        """渲染 Prometheus 文本格式，末尾带换行。

        顺序：先 uptime，再按名称升序依次输出 counters、gauges、histograms。
        """
        with self._lock:
            lines = [
                "# HELP fuguoai_uptime_seconds 服务运行时长（秒）",
                "# TYPE fuguoai_uptime_seconds gauge",
                f"fuguoai_uptime_seconds {time.time() - self._start_ts:.0f}",
            ]

            for name in sorted(self._counters):
                if name in self._help:
                    lines.append(f"# HELP {name} {self._help[name]}")
                lines.append(f"# TYPE {name} counter")
                for key, val in self._counters[name].items():
                    lines.append(f"{name}{_render_labels(key)} {_fmt(val)}")

            for name in sorted(self._gauges):
                if name in self._help:
                    lines.append(f"# HELP {name} {self._help[name]}")
                lines.append(f"# TYPE {name} gauge")
                for key, val in self._gauges[name].items():
                    lines.append(f"{name}{_render_labels(key)} {_fmt(val)}")

            for name in sorted(self._histograms):
                if name in self._help:
                    lines.append(f"# HELP {name} {self._help[name]}")
                lines.append(f"# TYPE {name} histogram")
                for key, h in self._histograms[name].items():
                    base_labels = dict(key)
                    for i, ub in enumerate(_LATENCY_BUCKETS):
                        le = "+Inf" if ub == float("inf") else _fmt(ub)
                        bl = dict(base_labels)
                        bl["le"] = le
                        bucket_labels = _render_labels(tuple(sorted(bl.items())))
                        lines.append(f"{name}_bucket{bucket_labels} {h['buckets'][i]}")
                    lines.append(f"{name}_sum{_render_labels(key)} {_fmt(h['sum'])}")
                    lines.append(f"{name}_count{_render_labels(key)} {h['count']}")

            return "\n".join(lines) + "\n"


METRICS = _Metrics()


def declare_core_metrics():
    """预声明 fuguoai 核心业务指标的 HELP/TYPE 并作用于全局单例 ``METRICS``。

    解决计数器未预声明时在首次上报前不会出现在 ``/metrics`` 中、且缺少 HELP 行的问题；
    本函数幂等，可重复调用（如多次进程初始化、热加载）而无副作用差异。
    """
    METRICS.declare_counter("fuguoai_persona_injections_total", "人格画像注入对话的次数(按设备)")
    METRICS.declare_counter("fuguoai_persona_api_total", "人格管理 API 调用次数(按方法/设备)")
    METRICS.declare_counter("fuguoai_eval_total", "对话自评估完成计数")
    METRICS.declare_gauge("fuguoai_eval_last_overall", "每台设备最近一次对话自评估总体分(1-5)")
    METRICS.declare_counter("fuguoai_eval_low_quality_total", "对话自评估总体分低于阈值的计数(质量退化信号)")
    METRICS.declare_counter("fuguoai_eval_safety_alerts_total", "对话自评估安全维度低于阈值的计数(安全红线)")
    METRICS.declare_counter("fuguoai_eval_errors_total", "对话自评估异常计数")
