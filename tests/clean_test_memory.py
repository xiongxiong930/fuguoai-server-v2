import yaml, os, tempfile, sys

MP = "/opt/fuguoai-server/data/.memory.yaml"
IP = "/opt/fuguoai-server/data/.memory_index.yaml"
# 测试角色隔离文件（mem_local_short 路由 test-/smoke- 设备到此）；纯测试产物，直接删。
MPT = "/opt/fuguoai-server/data/.memory.test.yaml"
IPT = "/opt/fuguoai-server/data/.memory_index.test.yaml"
PREFIXES = ("test", "smoke")  # 归一化后测试设备ID为 testxxx/smokexxx（无连字符/下划线）

def is_test(k):
    return str(k).lower().startswith(PREFIXES)

def atomic(path, data):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise

removed_mem = removed_idx = 0
if os.path.exists(MP):
    mem = yaml.safe_load(open(MP, encoding="utf-8", errors="replace")) or {}
    if not isinstance(mem, dict): mem = {}
    before = len(mem)
    mem = {k: v for k, v in mem.items() if not is_test(k)}
    removed_mem = before - len(mem)
    atomic(MP, mem)

if os.path.exists(IP):
    idx = yaml.safe_load(open(IP, encoding="utf-8", errors="replace")) or {}
    if not isinstance(idx, dict): idx = {}
    before = len(idx)
    idx = {k: v for k, v in idx.items() if not is_test(k)}
    removed_idx = before - len(idx)
    atomic(IP, idx)

# 测试隔离文件整份删除（纯产物，下次测试运行会自动重建）
for tp in (MPT, IPT):
    if os.path.exists(tp):
        try:
            os.unlink(tp)
            print(f"removed test file: {tp}")
        except Exception as e:
            print(f"warn: cannot remove {tp}: {e}")

print(f"cleaned .memory.yaml: removed {removed_mem} test roles, kept {len(mem)}")
print(f"cleaned .memory_index.yaml: removed {removed_idx} test roles, kept {len(idx)}")
print("kept roles:", list(mem.keys()))
