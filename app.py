"""fuguoai 服务端主入口（复果原生实现）。

进程级启动编排：配置加载 → 资产自检 → 指标预声明 → auth_key 解析 →
WebSocket / HTTP(OTA) 双服务拉起 → 事件循环看门狗（冻死自杀重启）→ 配置热加载 → 阻塞至退出。

本协议契约由后端定义，固件侧对齐此实现。看门狗、faulthandler 栈回溯、V5 配置单例、
GC 管理器、双配置陷阱、auth_key 三级优先级、MCP endpoint 转换均为 fuguoai 自有演进。
"""

import sys
import os
import time
import uuid
import signal
import threading
import faulthandler
import asyncio

from fuguoai_config.settings import load_config
from fuguoai_config.logger import setup_logging
from fuguoai_core.utils.util import get_local_ip, validate_mcp_endpoint, check_ffmpeg_installed
from fuguoai_core.utils import config_holder
from fuguoai_core.utils.config_holder import start_watchdog
from fuguoai_core.http_server import SimpleHttpServer
from fuguoai_core.websocket_server import WebSocketServer
from fuguoai_core.utils.gc_manager import get_gc_manager

TAG = __name__
logger = setup_logging()


# 启用 faulthandler：将来若事件循环再次冻死，可向进程发 SIGUSR1 取得全线程栈回溯，
# 直接定位卡死协程/锁。all_threads=True 同时 dump 看门狗等后台线程。
try:
    faulthandler.enable(file=sys.stderr, all_threads=True)
except Exception as _faulthandler_err:  # 极端环境（如已启用）不阻断启动
    logger.bind(tag=TAG).debug("faulthandler 启用跳过: {}", _faulthandler_err)


# ---------------------------------------------------------------------------
# 进程级自愈看门狗（防御事件循环冻死 = 进程不死但服务全瘫）
# ---------------------------------------------------------------------------
# 事件循环每隔约 1s 更新 _HEARTBEAT_STAMP（单调时钟）。
# 独立守护线程每数秒检查：若心跳超过 FREEZE_TIMEOUT 未跳动，说明 loop 已冻死，
# 立即 dump 栈并 os._exit(1)，docker 的 unless-stopped 策略会立刻重启容器。
# 这能覆盖"死锁/同步阻塞卡死 loop"这类进程不死但服务挂掉的事故（本次死锁同类）。
_HEARTBEAT_STAMP = 0.0        # time.monotonic() 秒；由 loop 协程定期刷新
_WATCHDOG_ARMED = False       # 仅在心跳协程启动后置 True，避免启动期误杀
FREEZE_TIMEOUT = 30.0         # loop 静默超过此秒数即判定冻死；留足正常重负载余量
_BOOT_GRACE = 20.0            # 启动后宽限期（秒），期间不触发，防模块导入到心跳启动间空窗


async def _heartbeat_tick() -> None:
    """事件循环心跳：仅证明 loop 仍被调度。loop 一旦冻死本协程便不再被运行。"""
    global _HEARTBEAT_STAMP
    _HEARTBEAT_STAMP = time.monotonic()
    while True:
        _HEARTBEAT_STAMP = time.monotonic()
        await asyncio.sleep(1.0)


def _freeze_watchdog() -> None:
    """守护线程：检测 loop 冻死并触发自杀式退出，交由 docker 重启。"""
    global _WATCHDOG_ARMED
    boot_at = time.monotonic()
    while True:
        time.sleep(5.0)
        now = time.monotonic()
        if now - boot_at < _BOOT_GRACE:       # 启动宽限期内不动作
            continue
        if not _WATCHDOG_ARMED:               # 心跳协程尚未开始不动作
            continue
        last = _HEARTBEAT_STAMP
        if last > 0.0 and (now - last) > FREEZE_TIMEOUT:
            try:
                logger.bind(tag=TAG).critical(
                    "事件循环疑似冻死（心跳静默 {:.1f}s > {:.1f}s），触发自杀重启",
                    now - last, FREEZE_TIMEOUT,
                )
                faulthandler.dump_traceback(all_threads=True)
            except Exception:
                pass
            # 直接退出（不走 atexit/清理），让 docker unless-stopped 立即重新拉起
            os._exit(1)


async def _drain_stdin() -> None:
    """消费标准输入里的回车，避免后台运行时 stdin 堆积触发 EOF 行为。

    标准库实现（executor 包阻塞 readline，与 aioconsole.ainput 同款机制），
    避免 GPL-3.0 组件随 requirements 进入生产镜像。stdin EOF（后台/无终端）
    时长眠等待，不忙转、不让协程崩溃。
    """
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if line == "":
            await asyncio.sleep(3600)


def _decide_auth_key(settings: dict) -> str:
    """解析 auth_key 三级优先级：server.auth_key > manager-api.secret > 随机生成。

    含占位符「你」的配置视为无效，回退到更低优先级。前两级均无效时生成随机
    uuid 并告警——重启后旧设备 token 将全部失效。
    """
    key = settings["server"].get("auth_key", "")
    if not key or len(key) == 0 or "你" in key:
        key = settings.get("manager-api", {}).get("secret", "")
        if not key or len(key) == 0 or "你" in key:
            key = str(uuid.uuid4().hex)
            logger.bind(tag=TAG).warning(
                "⚠️ auth_key 未配置且 manager-api.secret 缺失，已生成随机 auth_key："
                "重启后旧设备 token 将全部失效，生产环境务必在 server.auth_key 显式配置"
            )
    return key


def _rectify_mcp_endpoint(settings: dict) -> None:
    """规整 mcp_endpoint：校验通过后把 /mcp/ 接入点改写为 /call/ 调用点；否则置占位符。

    仅当配置了非占位 endpoint 时才处理；缺省（None）或仍含占位符「你」时保持原样。
    """
    endpoint = settings.get("mcp_endpoint", None)
    if endpoint is None or "你" in endpoint:
        return
    if validate_mcp_endpoint(endpoint):
        logger.bind(tag=TAG).info("mcp接入点是\t{}", endpoint)
        settings["mcp_endpoint"] = endpoint.replace("/mcp/", "/call/")
    else:
        logger.bind(tag=TAG).error("mcp接入点不符合规范")
        settings["mcp_endpoint"] = "你的接入点 websocket地址"


def _install_self_heal() -> None:
    """拉起事件循环心跳协程 + 看门狗守护线程（loop 冻死则自杀，docker 重启）。"""
    global _WATCHDOG_ARMED, _HEARTBEAT_STAMP
    _HEARTBEAT_STAMP = time.monotonic()
    asyncio.create_task(_heartbeat_tick())
    watcher = threading.Thread(target=_freeze_watchdog, name="loop-watchdog", daemon=True)
    watcher.start()
    _WATCHDOG_ARMED = True
    logger.bind(tag=TAG).info("事件循环看门狗已启动（冻死超时={}s）", FREEZE_TIMEOUT)


def _wire_crash_reporters(tasks: dict) -> None:
    """给后台任务挂异常回调，避免 WebSocket / OTA 服务静默崩溃。"""
    def _report(task: asyncio.Task, label: str) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.bind(tag=TAG).error(f"{label} 后台任务异常退出: {exc}", exc_info=exc)

    for label, task in tasks.items():
        task.add_done_callback(lambda t, name=label: _report(t, name))


def _announce_listen_endpoints(settings: dict) -> None:
    """打印 OTA / 视觉 / WebSocket 三个对外地址（仅信息提示，不影响行为）。"""
    read_from_api = settings.get("read_config_from_api", False)
    http_port = int(settings["server"].get("http_port", 8003))
    if not read_from_api:
        logger.bind(tag=TAG).info(
            "OTA接口是\t\thttp://{}:{}/fuguoai/ota/",
            get_local_ip(), http_port,
        )
    logger.bind(tag=TAG).info(
        "视觉分析接口是\thttp://{}:{}/mcp/vision/explain",
        get_local_ip(), http_port,
    )
    ws_port = 8000
    srv = settings.get("server", {})
    if isinstance(srv, dict):
        ws_port = int(srv.get("port", 8000))
    logger.bind(tag=TAG).info(
        "Websocket地址是\tws://{}:{}/fuguoai/v1/",
        get_local_ip(), ws_port,
    )
    logger.bind(tag=TAG).info(
        "=======上面的地址是websocket协议地址，请勿用浏览器访问======="
    )
    logger.bind(tag=TAG).info(
        "如想测试websocket请启动digital-human模块，打开浏览器交互测试"
    )
    logger.bind(tag=TAG).info(
        "=============================================================\n"
    )


async def wait_for_exit() -> None:
    """阻塞直到收到 Ctrl‑C / SIGTERM（Unix 用 add_signal_handler；Windows 靠 KeyboardInterrupt 冒泡）。"""
    loop = asyncio.get_running_loop()
    halt = asyncio.Event()
    if sys.platform == "win32":
        # Windows 无 add_signal_handler，await 永远 pending 的 fut，让 Ctrl‑C 冒泡到 asyncio.run
        try:
            await asyncio.Future()
        except KeyboardInterrupt:  # Ctrl‑C
            pass
        return
    for sig in (signal.SIGINT, signal.SIGTERM):  # Unix / macOS
        loop.add_signal_handler(sig, halt.set)
    await halt.wait()


async def main():
    check_ffmpeg_installed()
    settings = await load_config()

    # V5：注入全局配置单例，作为唯一真相源。settings 重新指向共享 dict 对象，
    # 后续对 settings 的修改（auth_key / mcp_endpoint）都落在共享对象上，
    # reload 时 WebSocketServer / connection / memory 持有的同一引用自动见新值，实现零重启。
    config_holder.set_config(settings)
    settings = config_holder.get_config()

    # 资产启动自检：关键音频缺失则启动即失败（防 B1 类静默失效）
    try:
        from fuguoai_config.paths import verify_critical_assets
        verify_critical_assets()
    except FileNotFoundError as e:
        logger.critical("资产自检失败: %s", e)
        raise

    # 指标预声明：persona/eval 计数器启动即注册到 /metrics（进位 1.0.0）
    try:
        from fuguoai_core.utils.metrics import declare_core_metrics
        declare_core_metrics()
    except Exception as e:
        logger.warning("指标预声明失败(不影响启动): {}", e)

    # 双配置陷阱启动告警：根 config.yaml 与运行时 data/.config.yaml 不一致提醒
    try:
        from fuguoai_config.config_loader import check_dual_config_trap
        check_dual_config_trap(logger)
    except Exception as e:
        logger.debug("双配置陷阱检查跳过: {}", e)

    # auth_key 优先级：配置文件 server.auth_key > manager-api.secret > 自动生成
    # auth_key 用于 jwt 认证（视觉分析接口 / ota 接口 token / websocket 认证）
    settings["server"]["auth_key"] = _decide_auth_key(settings)

    # 启动 stdin 消费 + 全局 GC 管理器（5 分钟清理一次）
    stdin_task = asyncio.create_task(_drain_stdin())
    gc_manager = get_gc_manager(interval_seconds=300)
    await gc_manager.start()

    # 启动 WebSocket 与 Simple HTTP(OTA) 双服务
    ws_server = WebSocketServer(settings)
    ws_task = asyncio.create_task(ws_server.start())
    ota_server = SimpleHttpServer(settings)
    ota_task = asyncio.create_task(ota_server.start())

    # 进程级自愈：事件循环心跳 + 看门狗
    _install_self_heal()

    # V5：配置热加载 watchdog（轮询 data/.config.yaml 变更，自动零重启重载）
    start_watchdog(interval=5.0)

    # 工业级防护：后台任务异常必须可见，避免 WebSocket 服务器静默崩溃
    _wire_crash_reporters({"WebSocket": ws_task, "OTA/HTTP": ota_task})

    # 打印对外地址
    _announce_listen_endpoints(settings)

    # mcp endpoint 规整（/mcp/ → /call/）
    _rectify_mcp_endpoint(settings)

    try:
        await wait_for_exit()  # 阻塞直到收到退出信号
    except asyncio.CancelledError:
        print("任务被取消，清理资源中...")
    finally:
        # 停止全局GC管理器
        await gc_manager.stop()

        # 取消所有任务（关键修复点）
        stdin_task.cancel()
        ws_task.cancel()
        if ota_task:
            ota_task.cancel()

        # 等待任务终止（必须加超时）
        await asyncio.wait(
            [stdin_task, ws_task, ota_task] if ota_task else [stdin_task, ws_task],
            timeout=3.0,
            return_when=asyncio.ALL_COMPLETED,
        )
        print("服务器已关闭，程序退出。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("手动中断，程序终止。")
