"""ToolManager 真进化离线测试 (⑳)

钉死 unified_tool_manager.py 的四项真进化:
  E1 按 executor 隔离缓存: 单 executor 瞬时失败不污染其他工具, 且自动重试恢复
      (早前实现把"所有 executor 合并聚合"整体缓存, 任一失败 -> 该 executor 工具被静默
       丢弃且残缺聚合被永久缓存, 直到某次 invalidate)
  E2 错误脱敏(_sanitize_error): 丢多行 traceback + 密钥串脱敏 + 限长;
      execute_tool 异常 -> action=ERROR, response 给用户/LLM 的是脱敏文本,
      result 存完整细节, 全栈进日志(早前实现 except 无日志且 str(e) 直喂 LLM/用户)
  E3 METRICS: ok/error/notfound/no_executor 各打点 + 耗时观测
  E4 同名工具冲突被记录并计入 get_tool_statistics
"""
import asyncio
from unittest.mock import MagicMock, patch

from fuguoai_core.providers.tools.unified_tool_manager import (
    ToolManager,
    _sanitize_error,
)
from fuguoai_core.providers.tools.base.tool_types import ToolType, ToolDefinition
from fuguoai_core.providers.tools.base.tool_executor import ToolExecutor
from fuguoai_plugins.register import Action, ActionResponse


class FakeExec(ToolExecutor):
    def __init__(self, names, fail_load_times=0, execute_raises=None, execute_result=None):
        self._names = list(names)
        self.fail_load_times = fail_load_times
        self._load_calls = 0
        self.execute_raises = execute_raises
        self.execute_result = execute_result or ActionResponse(
            action=Action.RESPONSE, response="ok", result="ok"
        )
        self._tt = ToolType.SERVER_PLUGIN
        self.last_conn = None
        self.last_args = None

    def get_tools(self):
        self._load_calls += 1
        if self._load_calls <= self.fail_load_times:
            raise RuntimeError("transient load boom")
        return {
            n: ToolDefinition(name=n, description={"name": n}, tool_type=self._tt)
            for n in self._names
        }

    def has_tool(self, name):
        return name in self._names

    async def execute(self, conn, tool_name, arguments):
        self.last_conn = conn
        self.last_args = arguments
        if self.execute_raises:
            raise self.execute_raises
        return self.execute_result


def _tm_with(executors):
    tm = ToolManager(MagicMock())
    for tt, ex in executors.items():
        ex._tt = tt
        tm.register_executor(tt, ex)
    return tm


def test_e1_isolation_and_recovery():
    # A 健康; B 前两次加载失败, 第三次成功
    A = FakeExec(["a1", "a2"])
    B = FakeExec(["b1"], fail_load_times=2)
    tm = _tm_with({ToolType.SERVER_PLUGIN: A, ToolType.SERVER_MCP: B})

    # 第一次: B 失败 -> 仅返回 A 的工具, B 被隔离(不污染 A), 但本身缺失
    t1 = tm.get_all_tools()
    assert set(t1) == {"a1", "a2"}, t1
    assert tm._failed_executors == {ToolType.SERVER_MCP}

    # 第二次: B 仍失败 -> 仍只 A(A 的工具始终可用)
    t2 = tm.get_all_tools()
    assert set(t2) == {"a1", "a2"}

    # 第三次: B 恢复 -> A+B 全部就位, 失败标记清除, 聚合固化
    t3 = tm.get_all_tools()
    assert set(t3) == {"a1", "a2", "b1"}, t3
    assert not tm._failed_executors
    assert tm._cached_tools is not None
    print("[E1] 单 executor 失败隔离 + 自动重试恢复 ✅")


def test_e1_permanent_failure_no_poison():
    A = FakeExec(["a1"])
    B = FakeExec(["b1"], fail_load_times=99999)
    tm = _tm_with({ToolType.SERVER_PLUGIN: A, ToolType.SERVER_MCP: B})
    for _ in range(3):
        assert set(tm.get_all_tools()) == {"a1"}
    assert tm._failed_executors == {ToolType.SERVER_MCP}
    # 永久失败的 B 不应让 A 的工具消失
    print("[E1] 永久失败 executor 不污染健康 executor 工具 ✅")


def test_e2_sanitize():
    # 密钥脱敏
    s = _sanitize_error("KeyError: 'api_key'")
    assert "api_key" not in s, s
    assert "[REDACTED]" in s
    # 多行 traceback 收敛为单行
    tb = "Traceback (most recent call last):\n  File x.py, line 1\nValueError: bad"
    s2 = _sanitize_error(tb)
    assert "\n" not in s2, "应丢弃多行 traceback"
    # 限长
    assert len(_sanitize_error("X" * 500, limit=50)) <= 53
    # 空/None 安全
    assert _sanitize_error("") == "工具执行出错"
    print("[E2] 错误脱敏: 去多行/脱密钥/限长/空安全 ✅")


def test_e2_execute_error_surface():
    A = FakeExec(["a1"], execute_raises=RuntimeError("boom api_key leak"))
    tm = _tm_with({ToolType.SERVER_PLUGIN: A})
    with patch(
        "fuguoai_core.providers.tools.unified_tool_manager.METRICS", MagicMock()
    ) as m:
        res = asyncio.run(tm.execute_tool("a1", {}))
    assert res.action == Action.ERROR
    assert "api_key" not in res.response, res.response  # 不外泄
    assert "[REDACTED]" in res.response
    assert res.result == "boom api_key leak"  # 完整细节留 result
    err = [
        c
        for c in m.inc.call_args_list
        if c.args[0] == "fuguoai_tool_calls_total"
        and c.kwargs["labels"]["status"] == "error"
    ]
    assert err, "error 应打点"
    print("[E2] 执行异常: action=ERROR, response 脱敏, result 存全量, 指标打点 ✅")


def test_e3_ok_metrics_and_passthrough():
    A = FakeExec(
        ["a1"],
        execute_result=ActionResponse(action=Action.RESPONSE, response="hi", result="hi"),
    )
    tm = _tm_with({ToolType.SERVER_PLUGIN: A})
    with patch(
        "fuguoai_core.providers.tools.unified_tool_manager.METRICS", MagicMock()
    ) as m:
        res = asyncio.run(tm.execute_tool("a1", {"x": 1}))
    assert res.action == Action.RESPONSE and res.response == "hi"
    ok = [
        c
        for c in m.inc.call_args_list
        if c.args[0] == "fuguoai_tool_calls_total"
        and c.kwargs["labels"]["status"] == "ok"
    ]
    assert ok, "成功应打点 ok"
    assert m.observe.called, "应观测耗时"
    print("[E3] 成功调用: 原样透传 + ok 指标 + 耗时观测 ✅")


def test_e3_notfound_and_no_executor():
    tm = _tm_with({ToolType.SERVER_PLUGIN: FakeExec(["a1"])})
    with patch(
        "fuguoai_core.providers.tools.unified_tool_manager.METRICS", MagicMock()
    ) as m:
        r1 = asyncio.run(tm.execute_tool("nope", {}))
        assert r1.action == Action.NOTFOUND and "不存在" in r1.response
        assert any(
            c.kwargs["labels"]["status"] == "notfound"
            for c in m.inc.call_args_list
            if c.args[0] == "fuguoai_tool_calls_total"
        )
        # 无任何 executor 注册
        empty = ToolManager(MagicMock())
        r2 = asyncio.run(empty.execute_tool("x", {}))
        assert r2.action == Action.NOTFOUND
    print("[E3] notfound/no_executor 正确分类 + 打点 ✅")


def test_e4_name_collision_recorded():
    A = FakeExec(["dup"])
    B = FakeExec(["dup"])
    tm = _tm_with({ToolType.SERVER_PLUGIN: A, ToolType.SERVER_MCP: B})
    tools = tm.get_all_tools()
    assert "dup" in tools
    assert "dup" in tm._collisions
    stats = tm.get_tool_statistics()
    assert stats.get("_name_collisions", 0) >= 1
    print("[E4] 同名工具冲突被记录并计入统计 ✅")


if __name__ == "__main__":
    test_e1_isolation_and_recovery()
    test_e1_permanent_failure_no_poison()
    test_e2_sanitize()
    test_e2_execute_error_surface()
    test_e3_ok_metrics_and_passthrough()
    test_e3_notfound_and_no_executor()
    test_e4_name_collision_recorded()
    print("\nALL UNIFIED TOOL MANAGER EVOLUTION TESTS PASS ✅")
