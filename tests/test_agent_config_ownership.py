"""agent-config 写接口设备归属校验回归测试（M12 越权修复）。

前提：后端已在 127.0.0.1:8002 运行（与 test_fuguoai_backend.py 同模式，不自行起服务）。
验证：用户 A 拥有设备 D 时，A 的 PUT/DELETE 应 200；用户 B 对已绑定给 A 的设备 D 操作应 403。
通过 mint 与 config 相同的 JWT 密钥签发的 token 模拟两个登录用户，不依赖真实微信 code。
"""
import sys
import json
import time

sys.path.insert(0, "/opt/fuguoai-server")

import yaml
from fuguoai_config.config_loader import get_project_dir

cfg = yaml.safe_load(open(get_project_dir() + "config.yaml"))
SECRET = (cfg.get("miniprogram") or {}).get("jwt_secret")
if not SECRET:
    print("SKIP: miniprogram.jwt_secret 未配置，跳过归属校验测试")
    sys.exit(0)

import jwt
import urllib.request
import urllib.error

from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.agent_config_store import get_agent_config_store

BASE = "http://127.0.0.1:8002/api/v1/agent-config/"
_openidA = "own_test_A_%d" % int(time.time())
_openidB = "own_test_B_%d" % int(time.time())
_tokA = jwt.encode({"openid": _openidA, "user_id": _openidA}, SECRET, algorithm="HS256")
_tokB = jwt.encode({"openid": _openidB, "user_id": _openidB}, SECRET, algorithm="HS256")
_dev = "own_test_dev_%d" % int(time.time())


def _call(method, tok):
    req = urllib.request.Request(BASE + _dev, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Type", "application/json")
    if method == "PUT":
        req.data = json.dumps({"voice": "zh-CN-XiaoxiaoNeural"}).encode()
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code


def main():
    # 造数据：设备 D 绑定给用户 A
    DeviceBindingStore().set_status(_dev, "active", user_id=_openidA)
    results = {}
    results["A_PUT_own"] = _call("PUT", _tokA)
    results["B_PUT_other"] = _call("PUT", _tokB)
    results["A_DEL_own"] = _call("DELETE", _tokA)
    results["B_DEL_other"] = _call("DELETE", _tokB)
    print("ownership results:", json.dumps(results, ensure_ascii=False))
    # 清理
    DeviceBindingStore().set_status(_dev, "unbound", user_id=_openidA)
    get_agent_config_store().delete_config(_dev)
    ok = (
        results["A_PUT_own"] == 200
        and results["B_PUT_other"] == 403
        and results["A_DEL_own"] == 200
        and results["B_DEL_other"] == 403
    )
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
