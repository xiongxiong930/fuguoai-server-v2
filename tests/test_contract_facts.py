# -*- coding: utf-8 -*-
"""对外契约事实一致性（离线 L3）。

将三份对外文档（BACKEND_INTEGRATION_PROTOCOL / INTERCOM_DEVICE_INTEGRATION_CONTRACT
/ EVENTS_WS_COORDINATION）声明的核心接口事实钉死到当前代码，防止「文档↔代码」漂移——
我们曾人工核对三份文档零矛盾，此测试把该结论自动化长期守护：

  - 设备 WS 握手路径 /fuguoai/v1/         （红线：设备 8000 WS 协议不改格式）
  - events WS 路径 /api/v1/events          （红线：实时推送走 /api/v1/events）
  - events WS ?token= 兜底鉴权             （user_auth）
  - intercom session 识别字符串 "intercom" （对讲进入开关）
  - SetIntercom 指令                       （对讲设备指令）
  - 端口 8000(WS)/8002(HTTP+metrics)       （http_server + data 配置）

契约若变更，本测试须同步更新，提醒人工复核对外文档。仅用标准库读源码字符串断言，
无第三方依赖，可稳定跑在任何解释器。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


def _must(rel, sub, what):
    assert sub in _src(rel), f"契约事实缺失/漂移: {rel} 应含 '{sub}'（{what}）"


def test_ws_handshake_path():
    _must("fuguoai_core/http_server.py", "/fuguoai/v1/", "设备 WS 握手路径")


def test_events_ws_path():
    _must("fuguoai_core/api/events_routes.py", "/api/v1/events", "events WS 路径")


def test_events_token_fallback():
    _must("fuguoai_core/utils/user_auth.py", "token", "events WS ?token= 兜底鉴权")


def test_intercom_session_switch():
    _must("fuguoai_core/handle/helloHandle.py", "intercom", "intercom session 识别")


def test_setintercom_command():
    # SetIntercom 在对讲链路多处引用，断言核心处理文件含此指令
    _must("fuguoai_core/handle/helloHandle.py", "SetIntercom", "SetIntercom 对讲指令")


def test_ports():
    srv = _src("fuguoai_core/http_server.py")
    assert "8000" in srv, "WS 端口 8000 未在 http_server 出现"
    cfg_present = False
    try:
        cfg_present = "8002" in _src("data/.config.yaml")
    except FileNotFoundError:
        cfg_present = False
    assert "8002" in srv or cfg_present, "HTTP/metrics 端口 8002 未出现"


if __name__ == "__main__":
    test_ws_handshake_path()
    test_events_ws_path()
    test_events_token_fallback()
    test_intercom_session_switch()
    test_setintercom_command()
    test_ports()
    print("test_contract_facts: ALL PASS")
