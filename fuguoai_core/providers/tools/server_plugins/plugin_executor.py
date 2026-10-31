"""服务端插件工具执行器（复果 AI 自研）。

本模块把全局插件注册表中的函数，按"当前连接"的视角封装成一个标准的
:class:`ToolExecutor`，交给对话链路统一调度。与直接把函数丢给 LLM 不同，
这里叠加了若干工程化能力：

* 描述隔离 —— 每个连接只拿到属于自己的工具描述副本，绝不污染全局注册表；
* 失败可观测 —— 异常详情进日志与 ``ActionResponse.result``，交给 LLM 的
  永远是面向用户的自然话术；
* 参数容错 —— 按函数签名过滤 LLM 幻觉出的多余参数，提前识别缺失的必填项；
* 配置健壮性 —— 配置缺失只影响可选功能，必备插件始终可用；
* 超时兜底 —— 串行多请求累积超时时统一掐断，并埋点调用量/失败率/耗时。
"""

import asyncio
import copy
import inspect
import time
from typing import Any, Dict, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
from ..base import ToolDefinition, ToolExecutor, ToolType
from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import Action, ActionResponse, all_function_registry

try:  # 指标模块缺失不应影响插件可用性
    from fuguoai_core.utils.metrics import METRICS
except Exception:  # pragma: no cover - 兜底路径
    METRICS = None

TAG = __name__
logger = setup_logging()

# 必备插件：无论配置如何都应出现在工具清单里
_NECESSARY_FUNCTIONS = ("handle_exit_intent", "get_lunar")

# 需要注入 conn 的工具类型码（修改系统提示 / 系统控制 / IoT 控制）
_CONN_REQUIRED_TYPE_CODES = frozenset({3, 4, 5})

# 插件执行超时兜底（秒），可被 config["plugins"]["executor_timeout"] 覆盖
DEFAULT_EXECUTOR_TIMEOUT = 30.0

# 新闻源默认值（分号分隔）
_DEFAULT_NEWS_SOURCES = "澎湃新闻;百度热搜;财联社"

# 面向用户的降级话术：不暴露任何内部实现细节
FRIENDLY_ERROR = "这个功能刚才没能用上，等会儿再试试吧"
FRIENDLY_TIMEOUT = "这个功能响应有点慢，先跳过啦"
FRIENDLY_BAD_ARGS = "我没太听清需要的信息，能再说详细一点吗"


def _record(metric_method: str, *args: Any, **kwargs: Any) -> None:
    """把一次调用埋点出去；任何异常都不得影响主流程。"""
    if METRICS is None:
        return
    try:
        getattr(METRICS, metric_method)(*args, **kwargs)
    except Exception:
        pass


class ServerPluginExecutor(ToolExecutor):
    """服务端插件工具执行器。"""

    def __init__(self, conn: "ConnectionHandler") -> None:
        self.conn = conn
        self.config = conn.config or {}

    # ------------------------------------------------------------------
    # 对外执行入口
    # ------------------------------------------------------------------
    async def execute(
        self, conn: "ConnectionHandler", tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行一个服务端插件工具，结果以 ActionResponse 交还。"""
        item = all_function_registry.get(tool_name)
        if item is None:
            _record(
                "inc", "fuguoai_plugin_calls_total",
                labels={"tool": tool_name, "status": "notfound"},
                help_text="服务端插件调用次数",
            )
            return ActionResponse(
                action=Action.NOTFOUND, response=f"插件函数 {tool_name} 不存在"
            )

        wants_conn = self._func_wants_conn(item)
        safe_args, missing = self._filter_arguments(
            item.func, arguments, drop_first=wants_conn
        )
        if missing:
            logger.bind(tag=TAG).warning(
                f"插件 {tool_name} 缺少必填参数 {missing}，原始参数={arguments}"
            )
            _record(
                "inc", "fuguoai_plugin_calls_total",
                labels={"tool": tool_name, "status": "bad_args"},
                help_text="服务端插件调用次数",
            )
            return ActionResponse(
                action=Action.ERROR,
                result=f"missing required arguments: {missing}",
                response=FRIENDLY_BAD_ARGS,
            )

        started = time.monotonic()
        try:
            raw = item.func(conn, **safe_args) if wants_conn else item.func(**safe_args)

            # 兼容 async def 工具函数：统一加超时兜底
            if asyncio.iscoroutine(raw):
                raw = await asyncio.wait_for(raw, timeout=self._timeout())

            _record(
                "observe", "fuguoai_plugin_duration_seconds",
                time.monotonic() - started, labels={"tool": tool_name},
                help_text="服务端插件执行耗时",
            )
            _record(
                "inc", "fuguoai_plugin_calls_total",
                labels={"tool": tool_name, "status": "ok"},
                help_text="服务端插件调用次数",
            )
            return raw

        except asyncio.TimeoutError:
            # 超时可观测，且给用户自然话术，不暴露内部实现
            logger.bind(tag=TAG).error(
                f"插件 {tool_name} 执行超时（>{self._timeout()}s），"
                f"参数={safe_args}"
            )
            _record(
                "inc", "fuguoai_plugin_calls_total",
                labels={"tool": tool_name, "status": "timeout"},
                help_text="服务端插件调用次数",
            )
            return ActionResponse(
                action=Action.ERROR,
                result=f"plugin timeout after {self._timeout()}s",
                response=FRIENDLY_TIMEOUT,
            )

        except Exception as err:  # 完整堆栈进日志，内部异常不外泄给 LLM
            logger.bind(tag=TAG).exception(
                f"插件 {tool_name} 执行失败: "
                f"{type(err).__name__}: {err}，参数={safe_args}"
            )
            _record(
                "inc", "fuguoai_plugin_calls_total",
                labels={"tool": tool_name, "status": "error"},
                help_text="服务端插件调用次数",
            )
            return ActionResponse(
                action=Action.ERROR,
                result=f"{type(err).__name__}: {err}",
                response=FRIENDLY_ERROR,
            )

    # ------------------------------------------------------------------
    # 工具清单
    # ------------------------------------------------------------------
    def get_tools(self) -> Dict[str, ToolDefinition]:
        """返回当前连接启用的服务端插件工具定义。"""
        enabled = self._enabled_function_names()
        definitions: Dict[str, ToolDefinition] = {}
        for name in enabled:
            item = all_function_registry.get(name)
            if item is None:
                continue
            definitions[name] = ToolDefinition(
                name=name,
                description=self._describe(name, item),
                tool_type=ToolType.SERVER_PLUGIN,
            )
        return definitions

    def has_tool(self, tool_name: str) -> bool:
        """检查指定工具是否由服务端插件提供。"""
        return tool_name in all_function_registry

    # ------------------------------------------------------------------
    # 超时取值
    # ------------------------------------------------------------------
    def _timeout(self) -> float:
        """插件执行超时（秒），非法配置回落默认值。"""
        try:
            raw = (self.config.get("plugins") or {}).get("executor_timeout")
            value = float(raw)
        except (TypeError, ValueError, AttributeError):
            return DEFAULT_EXECUTOR_TIMEOUT
        return value if value > 0 else DEFAULT_EXECUTOR_TIMEOUT

    # ------------------------------------------------------------------
    # 内部：是否需要注入 conn
    # ------------------------------------------------------------------
    @staticmethod
    def _func_wants_conn(item: Any) -> bool:
        """按工具类型码判断是否需要在调用时注入 conn 作为首个参数。"""
        type_obj = getattr(item, "type", None)
        code = getattr(type_obj, "code", None)
        return code in _CONN_REQUIRED_TYPE_CODES

    # ------------------------------------------------------------------
    # 内部：参数净化（E3）
    # ------------------------------------------------------------------
    @staticmethod
    def _filter_arguments(
        func: Any, arguments: Any, drop_first: bool = False
    ) -> Tuple[Dict[str, Any], list]:
        """按函数签名净化 LLM 给出的参数。

        返回 (可安全传入的参数字典, 缺失的必填参数名列表)。
        函数若声明 ``**kwargs`` 则不过滤未知键；签名不可解析时原样放行。
        """
        args = dict(arguments) if isinstance(arguments, dict) else {}

        try:
            params = list(inspect.signature(func).parameters.values())
        except (TypeError, ValueError):
            return args, []

        if drop_first and params:
            params = params[1:]  # 首参 conn 由位置注入，不参与关键字匹配

        accepts_kwargs = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params
        )
        named = [
            p for p in params
            if p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        ]
        allowed = {p.name for p in named}

        safe_args = args if accepts_kwargs else {
            key: val for key, val in args.items() if key in allowed
        }
        missing = [
            p.name for p in named
            if p.default is inspect.Parameter.empty and p.name not in safe_args
        ]
        return safe_args, missing

    # ------------------------------------------------------------------
    # 内部：配置驱动的启用函数名（E4）
    # ------------------------------------------------------------------
    def _enabled_function_names(self) -> list:
        """必备插件恒启用；配置启用的函数若已注册也会被纳入。"""
        configured = self._configured_functions()
        merged = set(_NECESSARY_FUNCTIONS) | set(configured)
        return sorted(merged)

    def _configured_functions(self) -> list:
        """安全读取配置中启用的插件名列表，任一层缺失都不抛异常。"""
        try:
            selected = (self.config.get("selected_module") or {}).get("Intent")
            intent_node = (self.config.get("Intent") or {}).get(selected) or {}
            functions = intent_node.get("functions") or []
        except AttributeError:
            return []

        if isinstance(functions, str):
            return [functions]
        if isinstance(functions, (list, tuple, set)):
            return [fn for fn in functions if isinstance(fn, str)]
        try:
            return [fn for fn in list(functions) if isinstance(fn, str)]
        except TypeError:
            return []

    def _plugin_node(self, func_name: str) -> Dict[str, Any]:
        """取某插件的配置节点，缺失返回空字典。"""
        try:
            return (self.config.get("plugins") or {}).get(func_name) or {}
        except AttributeError:
            return {}

    # ------------------------------------------------------------------
    # 内部：描述构建（E1）
    # ------------------------------------------------------------------
    def _describe(self, func_name: str, item: Any) -> Any:
        """构造当前连接专属的工具描述，绝不改写全局注册表。

        仅在确有定制需求（自定义描述或新闻源）时才深拷贝，避免无谓开销。
        """
        node = self._plugin_node(func_name)
        custom_desc = node.get("description") or ""
        is_news = func_name == "get_news_from_newsnow"

        if not custom_desc and not is_news:
            return item.description  # 无定制，直接共享只读对象

        description = copy.deepcopy(item.description)

        if custom_desc:
            func_node = (
                description.get("function")
                if isinstance(description, dict) else None
            )
            if isinstance(func_node, dict):
                func_node["description"] = custom_desc

        if is_news:
            self._inject_news_sources(description, node)

        return description

    @staticmethod
    def _inject_news_sources(description: Dict[str, Any], node: Dict[str, Any]) -> None:
        """按连接配置写入新闻源参数说明，仅作用于描述副本。"""
        sources = node.get("news_sources") or _DEFAULT_NEWS_SOURCES
        rendered = str(sources).replace(";", "、")
        try:
            description["function"]["parameters"]["properties"]["source"][
                "description"
            ] = (
                f"新闻源的标准中文名称，例如{rendered}等。"
                "可选参数，如果不提供则使用默认新闻源"
            )
        except (KeyError, TypeError):
            pass
