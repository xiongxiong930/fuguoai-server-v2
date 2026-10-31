"""定向验证 v4 记忆（内存路由隔离 + 测试读合并）修复。
只跑 test_v4_memory / test_v4_rag，验证：
  1) 测试角色 test-v4-mem-1 的记忆落盘到 .memory.test.yaml（生产文件不被污染）
  2) 测试 helper 能从合并文件读到，断言通过
"""
import asyncio
import sys

sys.path.insert(0, "/opt/fuguoai-server")
import test_fuguoai_backend as T


async def run():
    print("== test_v4_memory ==")
    r1 = await T.test_v4_memory("test-v4-mem-1")
    print("v4_memory ->", "PASS" if r1 else "FAIL")
    print("== test_v4_rag ==")
    r2 = await T.test_v4_rag("test-v4-mem-1")
    print("v4_rag ->", "PASS" if r2 else "FAIL")
    return r1 and r2


if __name__ == "__main__":
    ok = asyncio.run(run())
    print("VERIFY_V4_OK=", ok)
    sys.exit(0 if ok else 1)
