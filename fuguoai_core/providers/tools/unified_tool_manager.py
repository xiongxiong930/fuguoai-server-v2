"""工具聚合与调度器（复果原生实现，clean-room 重写）。

把若干来源不同的执行器（服务端插件 / MCP / 设备 IoT 等）统一聚合成一张
工具表，并负责工具路由、并发执行、错误脱敏与可观测打点。

设计要点（行为必须保留）：
  * 按执行器隔离缓存：单个执行器瞬时失败不污染其余执行器，且下次访问自愈；
    仅当没有任何执行器处于失败态时才固化聚合缓存。
  * 错误文本脱敏：异常原样 ``str(e)`` 可能泄露密钥 / 完整 traceback，统一收敛为
    单行、脱敏、限长的安全描述。
"""

import re
import time
from typing import Any, Dict, List, Optional, Set

from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import Action as _Act, ActionResponse as _Resp
from .base import ToolDefinition as _Spec, ToolExecutor as _Runner, ToolType as _Kind

try:
    from fuguoai_core.utils.metrics import METRICS
except Exception:
    METRICS = None

_LOG_TAG = __name__


# 密钥形态正则：命中常见密钥字段即替换为 [REDACTED]（脱敏底线，勿删 api[_-]?key|）。
_SECRET_PATTERN = re.compile(
    r"(password|passwd|token|secret|api[_-]?key|sk-[A-Za-z0-9]{8,}|AKID[A-Za-z0-9]+)",
    re.IGNORECASE,
)


def _sanitize_error(text: str, limit: int = 300) -> str:
    """把异常文本收敛为单行、脱敏、限长的安全描述。"""
    if not text or not text.strip():
        return "工具执行出错"
    first_meaningful = ""
    for raw_line in text.strip().splitlines():
        trimmed = raw_line.strip()
        if trimmed:
            first_meaningful = trimmed
            break
    if not first_meaningful:
        first_meaningful = text.strip()
    redacted = _SECRET_PATTERN.sub("[REDACTED]", first_meaningful)
    if len(redacted) > limit:
        redacted = redacted[:limit] + "..."
    return redacted


class ToolManager:
    """统一工具管理器，聚合并调度所有类型的工具。"""

    def __init__(self, conn):
        self.conn = conn
        self.logger = setup_logging()
        self._registry: Dict[_Kind, _Runner] = {}
        self._solid: Optional[Dict[str, _Spec]] = None
        self._solid_desc: Optional[List[Dict[str, Any]]] = None
        self._per_exec: Dict[_Kind, Optional[Dict[str, _Spec]]] = {}
        self._failed_executors: Set[_Kind] = set()
        self._collisions: List[str] = []

    # ----------------------------------------------------------------- 查询

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self.get_all_tools()

    def get_tool_type(self, tool_name: str) -> Optional[_Kind]:
        definition = self.get_all_tools().get(tool_name)
        return definition.tool_type if definition else None

    def get_supported_tool_names(self) -> List[str]:
        return list(self.get_all_tools().keys())

    def get_function_descriptions(self) -> List[Dict[str, Any]]:
        if self._solid_desc is not None:
            return self._solid_desc
        tools = self.get_all_tools()
        descriptions = [spec.description for spec in tools.values()]
        if self._solid is not None:
            self._solid_desc = descriptions
        return descriptions

    # ----------------------------------------------------------- 注册/刷新

    def register_executor(self, tool_type: _Kind, executor: _Runner):
        self._registry[tool_type] = executor
        self._wipe_cache()
        self.logger.debug(f"注册工具执行器: {tool_type.value}")

    def refresh_tools(self):
        self._wipe_cache()
        self.logger.debug("工具缓存已刷新")

    def _wipe_cache(self):
        self._solid = None
        self._solid_desc = None
        self._per_exec = {}
        self._failed_executors = set()

    # ------------------------------------------------------- 聚合工具表

    def get_all_tools(self) -> Dict[str, _Spec]:
        if self._solid is not None:
            return self._solid
        agg: Dict[str, _Spec] = {}
        for kind, runner in self._registry.items():
            bundle = self._load_one(kind, runner)
            if bundle is None:
                continue
            for tname, spec in bundle.items():
                if tname in agg:
                    self._collisions.append(tname)
                    self.logger.warning(
                        f"工具名称冲突: {tname} (类型 {kind.value} 覆盖先前)"
                    )
                agg[tname] = spec
        if not self._failed_executors:
            self._solid = agg
        return agg

    def _load_one(self, kind: _Kind, runner: _Runner) -> Optional[Dict[str, _Spec]]:
        cached = self._per_exec.get(kind)
        if cached is not None:
            return cached
        try:
            loaded = runner.get_tools()
        except Exception as exc:
            self._failed_executors.add(kind)
            self.logger.bind(tag=_LOG_TAG).error(
                f"获取{kind.value}工具失败(下次自动重试): "
                f"{type(exc).__name__}: {exc}"
            )
            if METRICS is not None:
                METRICS.inc(
                    "fuguoai_tool_load_errors_total",
                    1.0,
                    labels={"executor": kind.value},
                )
            return None
        self._failed_executors.discard(kind)
        self._per_exec[kind] = loaded
        return loaded

    # ----------------------------------------------------------- 执行

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> _Resp:
        started = time.monotonic()
        try:
            kind = self.get_tool_type(tool_name)
            if not kind:
                self._emit_metrics(tool_name, "notfound", None)
                return _Resp(action=_Act.NOTFOUND, response=f"工具 {tool_name} 不存在")
            runner = self._registry.get(kind)
            if not runner:
                self._emit_metrics(tool_name, "no_executor", None)
                return _Resp(
                    action=_Act.ERROR,
                    response=f"工具类型 {kind.value} 的执行器未注册",
                )
            self.logger.info(f"执行工具: {tool_name}，参数: {arguments}")
            outcome = await runner.execute(self.conn, tool_name, arguments)
            elapsed = time.monotonic() - started
            self._emit_metrics(tool_name, "ok", elapsed)
            self.logger.debug(f"工具执行结果: {outcome}")
            return outcome
        except Exception as exc:
            elapsed = time.monotonic() - started
            detail = str(exc)
            self._emit_metrics(tool_name, "error", elapsed)
            self.logger.bind(tag=_LOG_TAG).exception(f"执行工具 {tool_name} 时出错")
            return _Resp(action=_Act.ERROR, response=_sanitize_error(detail), result=detail)

    def _emit_metrics(self, tool_name: str, status: str, duration: Optional[float]):
        if METRICS is None:
            return
        METRICS.inc(
            "fuguoai_tool_calls_total",
            1.0,
            labels={"tool": tool_name, "status": status},
        )
        if duration is not None:
            METRICS.observe(
                "fuguoai_tool_duration_seconds",
                duration,
                labels={"tool": tool_name},
            )

    # ----------------------------------------------------- 统计与维护

    def get_tool_statistics(self) -> Dict[str, int]:
        stats: Dict[str, int] = {}
        for kind, runner in self._registry.items():
            try:
                stats[kind.value] = len(runner.get_tools())
            except Exception as exc:
                self.logger.bind(tag=_LOG_TAG).error(
                    f"获取{kind.value}工具统计时出错: {exc}"
                )
                stats[kind.value] = 0
        if self._collisions:
            stats["_name_collisions"] = len(self._collisions)
        return stats
