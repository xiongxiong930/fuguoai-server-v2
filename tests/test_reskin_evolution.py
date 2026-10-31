# -*- coding: utf-8 -*-
"""前两天「换皮」文件的真进化离线回归（2026-07-29）

锁定三处从"等价改写"升级为"真实演进"的改动不被回退：

  R1  get_ip_info 加超时 —— 外部 IP 归属地服务挂起时不再无限冻结事件循环。
       该函数在天气预热路径里跑在事件循环上（get_weather → call_soon_threadsafe
       → loop），无 timeout 即整进程 WS / 对讲 relay / events 卡死。
  R2  voiceprint 健康检查统一 aiohttp —— 移除同步 requests 依赖，构造期网络调用
       受 aiohttp ClientTimeout 硬约束，且全模块不再依赖第二套 HTTP 栈。
  R3  context 多 provider 并发拉取 —— httpx.AsyncClient + gather，预热延迟由
       各 provider 之和降为最慢单个（N×3s → 3s），单个 provider 失败不影响其余。

无真实网络依赖（全部以桩短路），供 tools/regression_gate.py L3 调用。
"""
import asyncio
import sys
import time
import threading
import json
import os
import tempfile
from unittest.mock import patch, MagicMock

from fuguoai_core.utils import util as util_mod
from fuguoai_core.utils import voiceprint_provider as vp_mod
from fuguoai_core.utils import context_provider as cp_mod

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


# ── 测试桩 ───────────────────────────────────────────────────────────────

class _Log:
    def bind(self, **kw):
        return self
    def warning(self, *a, **k):
        pass
    def error(self, *a, **k):
        pass
    def info(self, *a, **k):
        pass
    def debug(self, *a, **k):
        pass


# ── R1 get_ip_info 超时硬约束 ───────────────────────────────────────────

def test_get_ip_info_timeout():
    print("[R1] get_ip_info 超时硬约束（不冻结事件循环）")
    fake_ip = "8.8.8.8"

    captured = {}

    def fake_get(url, **kw):
        captured.update(kw)
        raise util_mod.requests.exceptions.Timeout("simulated hang")

    with patch("fuguoai_core.utils.cache.manager.cache_manager") as cm:
        cm.get.return_value = None  # 缓存未命中，必走网络
        with patch.object(util_mod.requests, "get", side_effect=fake_get):
            result = [None]

            def target():
                result[0] = util_mod.get_ip_info(fake_ip, _Log())

            t = threading.Thread(target=target)
            t.start()
            t.join(timeout=8)  # 若没加 timeout，这里会一直阻塞
            assert not t.is_alive(), (
                "get_ip_info 在无 timeout 时无限阻塞 —— 事件循环将被冻结"
            )
            assert result[0] == {}, f"超时须降级返回空字典, 实际 {result[0]}"
    # 演进生效的铁证：确实给 requests.get 传了 timeout
    assert "timeout" in captured and captured["timeout"] > 0, (
        f"必须给 requests.get 传 timeout, 实际 kwargs={captured}"
    )
    ok(f"外部 IP 服务超时降级返回 {{}} 且未挂起（timeout={captured['timeout']}s）")


# ── R2 voiceprint 仅依赖 aiohttp（无同步 requests） ─────────────────────

def _make_vp(config, data_dir):
    """构造 provider 并把注册表落盘目录指到临时目录，避免触碰 /opt。"""
    vp_mod._DATA_DIR = data_dir
    vp_mod._REGISTRY_PATH = os.path.join(data_dir, "voiceprint_registry.json")
    return vp_mod.VoiceprintProvider(config)


def test_voiceprint_health_unified_aiohttp():
    print("[R2] voiceprint 模块仅依赖 aiohttp（无同步 requests）")
    # 演进铁证：模块不再依赖第二套 HTTP 栈
    assert not hasattr(vp_mod, "requests"), "voiceprint_provider 应不依赖同步 requests"

    tmp = tempfile.mkdtemp(prefix="vp_test_")

    # 配置就绪（密钥齐全）→ enabled=True，不做任何启动期网络探测（无阻塞）
    cfg_ok = {"provider": "tencent", "secret_id": "AKID_test", "secret_key": "SK_test", "group_id": "fam_01"}
    p_ok = _make_vp(cfg_ok, tmp)
    assert p_ok.enabled is True, "密钥齐全应 enabled=True"
    ok("配置就绪 → enabled=True（无启动期网络阻塞）")

    # 缺密钥 → enabled=False，优雅降级
    cfg_bad = {"provider": "tencent", "secret_id": "", "secret_key": "", "group_id": ""}
    p_bad = vp_mod.VoiceprintProvider(cfg_bad)
    assert p_bad.enabled is False, "缺密钥应 enabled=False"
    ok("缺密钥 → enabled=False（优雅降级）")

    # 空注册表下 identify_speaker 返回 None，且不应触网（用桩确认 aiohttp 未被调用）
    net_calls = []
    class _NoNet:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, *a, **k):
            net_calls.append(a)
            raise AssertionError("空注册表下不应发起网络请求")
    with patch.object(vp_mod.aiohttp, "ClientSession", _NoNet):
        res = asyncio.run(p_ok.identify_speaker(b"RIFF....wav", "sess1"))
    assert res is None, f"空注册表应返回 None, 实际 {res}"
    assert not net_calls, "空注册表不应发起网络请求"
    ok("空注册表 identify 返回 None 且零网络请求")

    # enroll 走 aiohttp（非 requests）：桩一个成功响应
    class _EnrollResp:
        def __init__(self, status, payload):
            self.status = status
            self._p = payload
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def text(self):
            return json.dumps(self._p)
    class _EnrollSession:
        def __init__(self, resp):
            self._r = resp
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def post(self, url, **kw):
            return self._r
    resp = _EnrollResp(200, {"Response": {"Data": {"VoicePrintId": "vp_123", "SpeakerNick": "小明"}}})
    with patch.object(vp_mod.aiohttp, "ClientSession", lambda *a, **k: _EnrollSession(resp)):
        out = asyncio.run(p_ok.enroll_speaker("小明", b"RIFFwavdata"))
    assert out.get("ok") is True, f"enroll 应成功, 实际 {out}"
    assert out.get("voiceprint_id") == "vp_123"
    ok("enroll 经 aiohttp 成功并返回 VoicePrintId")

    # 清理临时注册表文件
    try:
        os.remove(vp_mod._REGISTRY_PATH)
    except OSError:
        pass


# ── R3 context 多 provider 并发拉取 ─────────────────────────────────────

class _CtxResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
    def json(self):  # httpx 的 Response.json() 是同步方法
        return self._payload


class _CtxClient:
    def __init__(self, sleep_s):
        self._sleep = sleep_s
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def get(self, url, headers=None, timeout=None):
        if self._sleep:
            await asyncio.sleep(self._sleep)  # 模拟服务端处理延迟
        return _CtxResp({"code": 0, "data": {"src": url}, "msg": ""})


def test_context_concurrent_fetch():
    print("[R3] context 多 provider 并发拉取（延迟由和降为最大）")
    sleep_s = 0.30
    providers = [
        {"url": "http://api.example.com/a"},
        {"url": "http://api.example.com/b"},
        {"url": "http://api.example.com/c"},
    ]
    cfg = {"context_providers": providers}
    with patch.object(cp_mod.httpx, "AsyncClient", lambda *a, **k: _CtxClient(sleep_s)):
        p = cp_mod.ContextDataProvider(cfg, logger=_Log())
        t0 = time.perf_counter()
        out = p.fetch_all("dev_123")
        dt = time.perf_counter() - t0
    # 3 个 provider 各 0.3s：顺序实现 ≈0.9s；并发实现 ≈0.3s
    assert dt < 0.6, f"并发拉取应≈最慢单个(0.3s), 实际 {dt:.3f}s（疑似回退为顺序）"
    for prov in providers:
        assert prov["url"] in out, f"结果应包含 provider {prov['url']}"
    ok(f"3 provider 并发 {dt * 1000:.0f}ms 返回（顺序实现会≈{len(providers) * sleep_s:.2f}s）")


def test_context_partial_failure_isolated():
    print("[R3b] context 单 provider 失败不影响其余")
    sleep_s = 0.05
    providers = [
        {"url": "http://api.example.com/ok"},
        {"url": "http://api.example.com/bad"},
    ]

    class _MixedClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, timeout=None):
            if url.endswith("/bad"):
                raise RuntimeError("boom")
            return _CtxResp({"code": 0, "data": {"src": url}, "msg": ""})

    with patch.object(cp_mod.httpx, "AsyncClient", lambda *a, **k: _MixedClient()):
        p = cp_mod.ContextDataProvider({"context_providers": providers}, logger=_Log())
        out = p.fetch_all("dev_123")
    assert "http://api.example.com/ok" in out, "正常 provider 结果应保留"
    assert "http://api.example.com/bad" not in out, "失败 provider 不应污染结果"
    ok("单 provider 异常被隔离，其余正常返回")


if __name__ == "__main__":
    test_get_ip_info_timeout()
    test_voiceprint_health_unified_aiohttp()
    test_context_concurrent_fetch()
    test_context_partial_failure_isolated()
    print(f"\nALL {PASS} CHECKS PASS ✓ (reskin evolution R1/R2/R3)")
    sys.exit(0)
