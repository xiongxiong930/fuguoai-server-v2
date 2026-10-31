# -*- coding: utf-8 -*-
"""复果AI · 运行期配置装载器

职责
----
- 组装全量配置：默认 ``config.yaml`` 叠加用户侧 ``data/.config.yaml``（本地深合并）；
  若用户侧配置了 manager-api，则改为从管理端接口拉取整棵配置树。
- 结果做进程级缓存（CacheType.CONFIG），避免重复的磁盘/网络 IO。
- 启动时按配置预创建日志与 ASR/TTS 输出目录。

对外契约（勿改签名）：get_project_dir / read_config / load_config /
get_config_from_api_async / get_private_config_from_api / ensure_directories /
merge_configs。
"""

import asyncio
import os
from collections.abc import Mapping

import yaml

from fuguoai_config.manage_api_client import (
    DeviceBindException,
    DeviceNotFoundException,
    get_agent_models,
    get_correct_words,
    get_server_config,
    init_service,
)

# 进程级缓存键
_CACHE_KEY = "main_config"


def get_project_dir():
    """返回仓库根目录（以本文件上两级为锚），末尾带斜杠。"""
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(here)) + "/"


def read_config(config_path):
    """读取单个 YAML 文件并解析为 Python 对象。"""
    with open(config_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def merge_configs(default_config, custom_config):
    """深合并两棵配置树，custom_config 一侧优先。

    规则：任一侧不是映射时直接取 custom 值；两侧均为映射时逐键递归下钻。
    """
    if not (isinstance(default_config, Mapping) and isinstance(custom_config, Mapping)):
        return custom_config

    out = dict(default_config)
    for key, incoming in custom_config.items():
        current = out.get(key)
        if key in out and isinstance(current, Mapping) and isinstance(incoming, Mapping):
            out[key] = merge_configs(current, incoming)
        else:
            out[key] = incoming
    return out


def ensure_directories(config):
    """按配置预创建各输出目录（幂等；无写权限时仅告警，不中断启动）。"""
    root = get_project_dir()
    wanted = set()

    # ① 日志目录（相对仓库根）
    log_dir = config.get("log", {}).get("log_dir", "tmp")
    wanted.add(os.path.join(root, log_dir))

    # ② ASR / TTS 每个 provider 声明的 output_dir（保持原样，不拼根目录）
    for section in ("ASR", "TTS"):
        block = config.get(section)
        if block is None:
            continue
        for provider_cfg in block.values():
            od = provider_cfg.get("output_dir", "")
            if od:
                wanted.add(od)

    # ③ selected_module 命中的 provider 目录（拼根目录）
    chosen = config.get("selected_module", {})
    for section in ("ASR", "LLM", "TTS"):
        picked = chosen.get(section)
        if not picked:
            continue
        if config.get(section) is None:
            continue
        if config.get(picked) is None:
            continue
        od = config.get(section, {}).get(picked, {}).get("output_dir")
        if od:
            wanted.add(os.path.join(root, od))

    for path in wanted:
        try:
            os.makedirs(path, exist_ok=True)
        except PermissionError:
            print(f"警告：无法创建目录 {path}，请检查写入权限")


async def get_config_from_api_async(config):
    """从管理端接口拉取全量配置，并以本地 server 段回填覆写。"""
    init_service(config)

    remote = await get_server_config()
    if remote is None:
        raise Exception("Failed to fetch server config from API")

    remote["read_config_from_api"] = True
    api_cfg = config["manager-api"]
    remote["manager-api"] = {
        "url": api_cfg.get("url", ""),
        "secret": api_cfg.get("secret", ""),
    }

    # auth 开关以远端为准；网络段（ip/端口等）以本地为准——远端不感知本机端口规划
    auth_on = remote.get("server", {}).get("auth", {}).get("enabled", False)
    local_server = config.get("server")
    if local_server:
        remote["server"] = {
            "ip": local_server.get("ip", ""),
            "port": local_server.get("port", ""),
            "http_port": local_server.get("http_port", ""),
            "vision_explain": local_server.get("vision_explain", ""),
            "auth_key": local_server.get("auth_key", ""),
        }
    remote["server"]["auth"] = {"enabled": auth_on}

    # 远端未下发提示词模板时回退本地
    if not remote.get("prompt_template"):
        remote["prompt_template"] = config.get("prompt_template")
    return remote


async def get_private_config_from_api(config, device_id, client_id):
    """并发拉取设备私有配置与纠错词表；私有配置失败按业务异常上抛。"""
    agent_result, words_result = await asyncio.gather(
        get_agent_models(device_id, client_id, config["selected_module"]),
        get_correct_words(device_id),
        return_exceptions=True,
    )

    if isinstance(agent_result, (DeviceNotFoundException, DeviceBindException)):
        raise agent_result

    private_config = {} if isinstance(agent_result, Exception) else agent_result
    if not isinstance(words_result, Exception) and words_result:
        private_config["correct_words"] = words_result
    return private_config


# 双配置陷阱：根 config.yaml 为模板/占位，运行时真实配置在 data/.config.yaml。
# 若运维误改根文件、或运行时文件缺失，启动告警提示，避免"改了不生效"。
# 注意：密钥类叶子键用"词尾锚定"匹配，避免 "token" 子串误伤 max_tokens /
# max_new_tokens 等普通配置项；sk- 前缀型密钥仍用子串匹配。
_SECRET_LEAF_HINTS_SUFFIX = ("auth_key", "secret", "api_key", "password",
                             "passwd", "access_key", "_token", "token")
_SECRET_LEAF_HINTS_SUBSTR = ("sk-",)


def _leaf_is_secret_key(k: str) -> bool:
    kl = k.lower()
    for h in _SECRET_LEAF_HINTS_SUFFIX:
        if kl.endswith(h):
            return True
    for h in _SECRET_LEAF_HINTS_SUBSTR:
        if h in kl:
            return True
    return False


def _is_real_secret(v) -> bool:
    if v is None or v == "":
        return False
    if isinstance(v, str):
        s = v.strip().lower()
        # 与 app.py 约定一致：含"你"（如"你的密钥"）即占位符，非真实密钥
        if "你" in s:
            return False
        # 明显的占位/示例值：含 xxx、或以 -xxx 结尾、或固定的本地令牌
        if "xxx" in s:
            return False
        if s in ("lm-studio", "your_key_here", "changeme", "placeholder",
                 "请替换", "<your-key>", "<在此填写>", "your-secret-here"):
            return False
    return True


def detect_dual_config_trap(base_cfg, user_cfg) -> list:
    """纯函数：返回根配置中含真实密钥、但运行时未覆盖的键路径列表（只读诊断）。"""
    flagged = []

    def scan(prefix, base_node, user_node):
        if not isinstance(base_node, dict):
            return
        unode = user_node if isinstance(user_node, dict) else {}
        for k, v in base_node.items():
            cur = prefix + k
            if isinstance(v, dict):
                scan(cur + ".", v, unode.get(k))
            elif _leaf_is_secret_key(k) and _is_real_secret(v) and k not in unode:
                flagged.append(cur)

    scan("", base_cfg or {}, user_cfg or {})
    return flagged


def check_dual_config_trap(logger) -> None:
    """启动告警（只读、不影响启动）：

    - 运行时 data/.config.yaml 缺失 → 警告当前回退根模板，改动会丢失；
    - 根 config.yaml 含真实密钥类值、但 data/.config.yaml 未覆盖该键 → 警告改错了文件。
    """
    try:
        root = get_project_dir()
        base = read_config(root + "config.yaml") or {}
    except Exception:
        return

    user_path = root + "data/.config.yaml"
    try:
        user = read_config(user_path) or {}
    except FileNotFoundError:
        logger.warning(
            "双配置陷阱：运行时配置 {} 缺失，当前回退根 config.yaml 模板默认值；"
            "改配置/密钥请改 data/.config.yaml，否则热加载或重启会丢失改动。",
            user_path,
        )
        return
    except Exception:
        return

    flagged = detect_dual_config_trap(base, user)
    if flagged:
        logger.warning(
            "双配置陷阱：根 config.yaml 含真实密钥但运行时 data/.config.yaml 未覆盖（{}）。"
            "密钥/配置改动请改 data/.config.yaml，否则下次热加载会被模板默认值冲掉。",
            ", ".join(flagged),
        )


async def load_config():
    """装载全量配置（带进程级缓存）。"""
    from fuguoai_core.utils.cache.manager import CacheType, cache_manager

    hit = cache_manager.get(CacheType.CONFIG, _CACHE_KEY)
    if hit is not None:
        return hit

    root = get_project_dir()
    loop = asyncio.get_running_loop()
    # 文件读取/目录创建属同步阻塞 I/O，丢线程池执行，避免阻塞 WS 共享事件循环
    # （reload/startup 时不再卡住 8000/8002 的 accept）。
    base_cfg, user_cfg = await asyncio.gather(
        loop.run_in_executor(None, read_config, root + "config.yaml"),
        loop.run_in_executor(None, read_config, root + "data/.config.yaml"),
    )

    if user_cfg.get("manager-api", {}).get("url"):
        config = await get_config_from_api_async(user_cfg)
    else:
        config = merge_configs(base_cfg, user_cfg)

    await loop.run_in_executor(None, ensure_directories, config)
    cache_manager.set(CacheType.CONFIG, _CACHE_KEY, config)
    return config
