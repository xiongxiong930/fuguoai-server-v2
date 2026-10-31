"""ServerPluginExecutor 真进化离线测试（phase5 / ⑲）

钉死:
  E1  跨连接描述隔离: get_tools 定制描述时深拷贝，全局 all_function_registry
      的 FunctionItem.description 保持原样；两个不同 config 的连接互不污染。
  E2  失败不泄露内部实现: 插件抛异常时交给 LLM 的 response 是自然话术，
      原始异常仅进 result（供日志/调试），不再是 str(e)。
  E3  LLM 幻觉参数容错: 未知参数被按签名过滤掉不再 TypeError；
      缺失必填参数提前识别为 bad_args 而非崩溃；**kwargs 函数不过滤。
  E4  配置健壮性: config 缺 Intent/selected_module 等任一层时不抛 KeyError，
      必备插件（handle_exit_intent/get_lunar）仍在工具清单里。
  E5  超时兜底: 协程插件挂死时按 executor_timeout 中断并返回友好话术。

本测试不依赖真实插件实现，直接向 all_function_registry 注入受控 FunctionItem。
"""
import asyncio
import copy
from unittest.mock import MagicMock

from fuguoai_plugins.register import (
    all_function_registry,
    FunctionItem,
    ToolType,
    Action,
)
import fuguoai_core.providers.tools.server_plugins.plugin_executor as pe_mod
from fuguoai_core.providers.tools.server_plugins.plugin_executor import (
    ServerPluginExecutor,
    FRIENDLY_ERROR,
    FRIENDLY_TIMEOUT,
    FRIENDLY_BAD_ARGS,
)


def _conn(config):
    c = MagicMock()
    c.config = config
    return c


def _base_config(functions=None, plugins=None):
    return {
        "selected_module": {"Intent": "intent_llm"},
        "Intent": {"intent_llm": {"functions": functions or []}},
        "plugins": plugins or {},
    }


def _register(name, func, tool_type=ToolType.WAIT, description=None):
    """注入受控插件，返回清理函数。"""
    desc = description if description is not None else {
        "type": "function",
        "function": {
            "name": name,
            "description": "原始描述",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "原始source描述"}
                },
            },
        },
    }
    item = FunctionItem(name, desc, func, tool_type)
    all_function_registry[name] = item

    def _cleanup():
        all_function_registry.pop(name, None)

    return item, _cleanup


# ---------------------------------------------------------------- E1
def test_e1_description_not_polluting_global():
    """定制描述必须作用于副本，全局注册表不被改写。"""
    item, cleanup = _register("fg_probe_desc", lambda: None)
    original = copy.deepcopy(item.description)
    try:
        cfg = _base_config(
            functions=["fg_probe_desc"],
            plugins={"fg_probe_desc": {"description": "设备A的定制描述"}},
        )
        tools = ServerPluginExecutor(_conn(cfg)).get_tools()

        assert tools["fg_probe_desc"].description["function"]["description"] == \
            "设备A的定制描述", "副本应带上定制描述"
        assert item.description == original, \
            "全局注册表的 description 不得被就地改写（跨连接污染）"
    finally:
        cleanup()


def test_e1_two_connections_isolated():
    """两个不同 config 的连接，工具描述互不串扰。"""
    item, cleanup = _register("fg_probe_iso", lambda: None)
    try:
        cfg_a = _base_config(
            functions=["fg_probe_iso"],
            plugins={"fg_probe_iso": {"description": "A的描述"}},
        )
        cfg_b = _base_config(
            functions=["fg_probe_iso"],
            plugins={"fg_probe_iso": {"description": "B的描述"}},
        )
        tools_a = ServerPluginExecutor(_conn(cfg_a)).get_tools()
        tools_b = ServerPluginExecutor(_conn(cfg_b)).get_tools()
        # 关键：先建立的 A 在 B 构造之后仍应保持自己的描述
        assert tools_a["fg_probe_iso"].description["function"]["description"] == "A的描述"
        assert tools_b["fg_probe_iso"].description["function"]["description"] == "B的描述"
    finally:
        cleanup()


def test_e1_news_sources_isolated():
    """新闻源参数说明同样只作用于副本。"""
    item, cleanup = _register("get_news_from_newsnow", lambda source=None: None)
    original = copy.deepcopy(item.description)
    try:
        cfg = _base_config(
            functions=["get_news_from_newsnow"],
            plugins={"get_news_from_newsnow": {"news_sources": "央视新闻;新华社"}},
        )
        tools = ServerPluginExecutor(_conn(cfg)).get_tools()
        rendered = tools["get_news_from_newsnow"].description["function"][
            "parameters"]["properties"]["source"]["description"]
        assert "央视新闻、新华社" in rendered, "分号应转为顿号并写入副本"
        assert item.description == original, "全局注册表不得被新闻源配置污染"
    finally:
        cleanup()


# ---------------------------------------------------------------- E2
def test_e2_exception_not_leaked_to_llm():
    """插件异常不得把内部实现细节交给 LLM。"""
    def _boom():
        raise KeyError("api_key")

    _, cleanup = _register("fg_probe_boom", _boom)
    try:
        executor = ServerPluginExecutor(_conn(_base_config()))
        resp = asyncio.run(executor.execute(_conn(_base_config()),
                                            "fg_probe_boom", {}))
        assert resp.action is Action.ERROR
        assert resp.response == FRIENDLY_ERROR, "交给LLM的应是自然话术"
        assert "api_key" not in str(resp.response), "内部异常细节不得外泄"
        assert "api_key" in str(resp.result), "原始异常应保留在 result 供排障"
    finally:
        cleanup()


# ---------------------------------------------------------------- E3
def test_e3_unknown_arguments_filtered():
    """LLM 幻觉出的多余参数应被过滤，而不是 TypeError。"""
    seen = {}

    def _tool(city=None):
        seen["city"] = city
        return "ok"

    _, cleanup = _register("fg_probe_args", _tool)
    try:
        executor = ServerPluginExecutor(_conn(_base_config()))
        resp = asyncio.run(executor.execute(
            _conn(_base_config()), "fg_probe_args",
            {"city": "深圳", "unit": "celsius", "lang": "zh"},
        ))
        assert resp == "ok", "过滤未知参数后应正常执行"
        assert seen["city"] == "深圳"
    finally:
        cleanup()


def test_e3_missing_required_argument():
    """缺失必填参数应提前识别为 bad_args，不进函数体。"""
    called = {"n": 0}

    def _tool(song_name):
        called["n"] += 1
        return "played"

    _, cleanup = _register("fg_probe_required", _tool)
    try:
        executor = ServerPluginExecutor(_conn(_base_config()))
        resp = asyncio.run(executor.execute(
            _conn(_base_config()), "fg_probe_required", {}))
        assert resp.action is Action.ERROR
        assert resp.response == FRIENDLY_BAD_ARGS
        assert called["n"] == 0, "必填参数缺失时不应真正调用插件"
        assert "song_name" in str(resp.result)
    finally:
        cleanup()


def test_e3_var_keyword_not_filtered():
    """声明 **kwargs 的插件不应被过滤掉参数。"""
    seen = {}

    def _tool(**kwargs):
        seen.update(kwargs)
        return "ok"

    _, cleanup = _register("fg_probe_kwargs", _tool)
    try:
        executor = ServerPluginExecutor(_conn(_base_config()))
        resp = asyncio.run(executor.execute(
            _conn(_base_config()), "fg_probe_kwargs", {"a": 1, "b": 2}))
        assert resp == "ok"
        assert seen == {"a": 1, "b": 2}, "**kwargs 函数应收到全部参数"
    finally:
        cleanup()


def test_e3_conn_param_skipped():
    """需要 conn 的工具，签名首参不参与关键字匹配与必填校验。"""
    seen = {}

    def _tool(conn, city=None):
        seen["conn"] = conn
        seen["city"] = city
        return "ok"

    _, cleanup = _register("fg_probe_conn", _tool, tool_type=ToolType.SYSTEM_CTL)
    try:
        conn = _conn(_base_config())
        executor = ServerPluginExecutor(conn)
        resp = asyncio.run(executor.execute(conn, "fg_probe_conn",
                                            {"city": "上海", "junk": 1}))
        assert resp == "ok"
        assert seen["conn"] is conn, "conn 应按位置注入"
        assert seen["city"] == "上海"
    finally:
        cleanup()


# ---------------------------------------------------------------- E4
def test_e4_broken_config_keeps_necessary_tools():
    """配置缺失不得让整组服务端插件静默消失。"""
    _, c1 = _register("handle_exit_intent", lambda conn, say_goodbye=None: None,
                      tool_type=ToolType.SYSTEM_CTL)
    _, c2 = _register("get_lunar", lambda date=None, query=None: None)
    try:
        for broken in ({}, {"selected_module": {}}, {"Intent": None},
                       {"selected_module": {"Intent": "nope"}, "Intent": {}}):
            tools = ServerPluginExecutor(_conn(broken)).get_tools()
            assert "handle_exit_intent" in tools and "get_lunar" in tools, \
                f"配置 {broken} 下必备插件不应消失"
    finally:
        c1()
        c2()


def test_e4_functions_as_string_or_garbage():
    """functions 配置为字符串/非法类型时不崩，按能力降级。"""
    _, cleanup = _register("fg_probe_cfg", lambda: None)
    try:
        cfg_str = _base_config(functions="fg_probe_cfg")
        tools = ServerPluginExecutor(_conn(cfg_str)).get_tools()
        assert "fg_probe_cfg" in tools, "单个字符串应被当作一个函数名"

        cfg_bad = _base_config(functions=12345)
        ServerPluginExecutor(_conn(cfg_bad)).get_tools()  # 不抛异常即通过
    finally:
        cleanup()


# ---------------------------------------------------------------- E5
def test_e5_timeout_fallback():
    """协程插件挂死时按超时中断并返回友好话术。"""
    async def _hang():
        await asyncio.sleep(10)
        return "never"

    _, cleanup = _register("fg_probe_hang", _hang)
    try:
        cfg = _base_config()
        cfg["plugins"]["executor_timeout"] = 0.1
        executor = ServerPluginExecutor(_conn(cfg))
        resp = asyncio.run(executor.execute(_conn(cfg), "fg_probe_hang", {}))
        assert resp.action is Action.ERROR
        assert resp.response == FRIENDLY_TIMEOUT
        assert "timeout" in str(resp.result).lower()
    finally:
        cleanup()


def test_e5_timeout_config_fallback():
    """超时配置非法时回落默认值，不抛异常。"""
    for bad in (None, "abc", -1, 0, {}):
        cfg = _base_config()
        cfg["plugins"]["executor_timeout"] = bad
        assert ServerPluginExecutor(_conn(cfg))._timeout() == \
            pe_mod.DEFAULT_EXECUTOR_TIMEOUT, f"非法超时 {bad} 应回落默认值"

    cfg = _base_config()
    cfg["plugins"]["executor_timeout"] = 5
    assert ServerPluginExecutor(_conn(cfg))._timeout() == 5.0


def test_notfound_contract_unchanged():
    """未注册工具仍返回 NOTFOUND，契约不变。"""
    executor = ServerPluginExecutor(_conn(_base_config()))
    resp = asyncio.run(executor.execute(_conn(_base_config()),
                                        "fg_definitely_missing", {}))
    assert resp.action is Action.NOTFOUND


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  [OK] {fn.__name__}")
    print(f"[PASS] plugin_executor 真进化测试全部通过（{len(tests)} 项）")


if __name__ == "__main__":
    main()
