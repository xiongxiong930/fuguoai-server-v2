"""mcp 命名空间桩 —— 仅供离线测试注入 sys.modules, 使 server_mcp/device_mcp/
unified_tool_handler 等依赖 `mcp` SDK 的模块可在不安装真实 mcp 的情况下导入。

为什么用桩而非装真实 mcp: 本仓代码按旧版 mcp API 编写(streamablehttp_client 弃用
别名 + ElicitationFnT), 与最新 mcp 2.0.0 不兼容; 真实 mcp 版本会在测试环境与生产环境
之间漂移。桩只提供模块/名字占位, 运行时行为由各测试自行 mock, 做到版本无关、零生产
API 改动风险。

用法: 在测试文件最顶部 `import mcp_stub`(其模块体即把桩注入 sys.modules),
之后任何 `from mcp... import ...` 都会命中本桩。
"""
import sys
import types


def _pkg(**attrs):
    m = types.ModuleType("mcp")
    m.__path__ = []  # 充当包, 避免 "not a package" 导入错误
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


# —— 代码 import 用到的名字占位(具体运行时对象由测试 mock) ——
ClientSession = object
StdioServerParameters = object
Implementation = object
SamplingFnT = object
ElicitationFnT = object
ListRootsFnT = object
LoggingFnT = object
MessageHandlerFnT = object
stdio_client = object
sse_client = object
streamablehttp_client = object
ProgressFnT = object
LoggingMessageNotificationParams = object

mcp = _pkg(
    ClientSession=ClientSession,
    StdioServerParameters=StdioServerParameters,
    Implementation=Implementation,
)
client_session = _pkg(
    SamplingFnT=SamplingFnT,
    ElicitationFnT=ElicitationFnT,
    ListRootsFnT=ListRootsFnT,
    LoggingFnT=LoggingFnT,
    MessageHandlerFnT=MessageHandlerFnT,
)
client_stdio = _pkg(stdio_client=stdio_client)
client_sse = _pkg(sse_client=sse_client)
client_streamable = _pkg(streamablehttp_client=streamablehttp_client)
shared_session = _pkg(ProgressFnT=ProgressFnT)
mcp_types = _pkg(LoggingMessageNotificationParams=LoggingMessageNotificationParams)

client = _pkg(
    session=client_session,
    stdio=client_stdio,
    sse=client_sse,
    streamable_http=client_streamable,
)
shared = _pkg(session=shared_session)

mcp.client = client
mcp.shared = shared
mcp.types = mcp_types

# 注入 sys.modules(仅当未真实安装时); 顺序: 父包先于子模块
for name, mod in [
    ("mcp", mcp),
    ("mcp.client", client),
    ("mcp.client.session", client_session),
    ("mcp.client.stdio", client_stdio),
    ("mcp.client.sse", client_sse),
    ("mcp.client.streamable_http", client_streamable),
    ("mcp.shared", shared),
    ("mcp.shared.session", shared_session),
    ("mcp.types", mcp_types),
]:
    sys.modules.setdefault(name, mod)
