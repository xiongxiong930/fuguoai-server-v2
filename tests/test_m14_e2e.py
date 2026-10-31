"""M14 端到端：在服务进程外验证 /api/v1/events 的鉴权 + WS 握手 + hello + ping/pong。

注意：事件旁路（mark_online→推送）跨进程不可复现（event_bus 是进程内单例），
该逻辑已由 test_m14_events.py 在同进程 100% 覆盖。本脚本验证「推送通道本身」：
  1) 无鉴权 GET → 401
  2) 带合法 JWT 的 WS 握手成功，收到 hello
  3) 客户端 ping → 服务端 pong
"""
import asyncio
import json
import sys
import urllib.request

import websockets

from fuguoai_core.utils.user_auth import issue_token

BASE_HTTP = "http://127.0.0.1:8002/api/v1/events"
BASE_WS = "ws://127.0.0.1:8002/api/v1/events"
PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")


def test_unauth():
    try:
        urllib.request.urlopen(BASE_HTTP, timeout=5)
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        code = f"ERR:{e}"
    check(f"1 无鉴权 GET → 401 (实得 {code})", code == 401)


async def test_ws():
    token = issue_token({"user_id": "m14_e2e_user", "openid": "m14_e2e_user"})
    headers = {"Authorization": f"Bearer {token}"}
    try:
        conn = websockets.connect(BASE_WS, additional_headers=headers, open_timeout=8)
    except TypeError:
        conn = websockets.connect(BASE_WS, extra_headers=headers, open_timeout=8)
    async with conn as ws:
        # hello
        raw = await asyncio.wait_for(ws.recv(), timeout=8)
        msg = json.loads(raw)
        check("2 WS 握手成功并收到 hello", msg.get("type") == "hello")
        check("3 hello 含 online 列表", isinstance(msg.get("online"), list))
        # ping → pong
        await ws.send(json.dumps({"type": "ping"}))
        for _ in range(3):  # 可能先来一条 server ping，最多读 3 条找 pong
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
            m = json.loads(raw)
            if m.get("type") == "pong":
                check("4 客户端 ping → 服务端 pong", True)
                break
        else:
            check("4 客户端 ping → 服务端 pong", False)


def main():
    test_unauth()
    try:
        asyncio.run(test_ws())
    except Exception as e:
        check(f"WS 测试异常: {e}", False)
    print("\n==== M14 E2E 汇总 ====")
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
