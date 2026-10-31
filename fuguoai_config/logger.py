"""日志基础设施（复果自有实现）。

通过 loguru 提供统一的结构化日志：控制台 + 按大小轮转的文件双通道，并在每条
日志上打「模块字符串 / tag / 设备ID」三处标签，使多设备、多模块的排障能在同一
行内归因。

``SERVER_VERSION`` 是版本标记，会嵌入每条日志与文件头；``setup_logging`` 仅在首次
调用时真正配置 loguru（后续调用直接返回已配置的 logger），避免重复 add handler。
"""
import os
import sys
import asyncio
from loguru import logger
from fuguoai_config.config_loader import load_config
from fuguoai_config.settings import check_config_file
from fuguoai_core.utils.cache.manager import cache_manager, CacheType

SERVER_VERSION = "1.2.0"
_logger_initialized = False


def get_module_abbreviation(module_name, module_dict):
    """取模块名的缩写：空值返回 '00'；含下划线取末段前两位；否则取前两位。"""
    module_value = module_dict.get(module_name, "")
    if not module_value:
        return "00"
    if "_" in module_value:
        parts = module_value.split("_")
        return parts[-1][:2] if parts[-1] else "00"
    return module_value[:2]


def build_module_string(selected_module):
    """按 VAD/ASR/LLM/TTS/Memory/Intent/VLLM 顺序拼出模块字符串。"""
    return (
        get_module_abbreviation("VAD", selected_module)
        + get_module_abbreviation("ASR", selected_module)
        + get_module_abbreviation("LLM", selected_module)
        + get_module_abbreviation("TTS", selected_module)
        + get_module_abbreviation("Memory", selected_module)
        + get_module_abbreviation("Intent", selected_module)
        + get_module_abbreviation("VLLM", selected_module)
    )


def formatter(record):
    """为缺省字段补默认值，并把 device_id 归一成 12 宽，便于多设备同行归因。"""
    record["extra"].setdefault("tag", record["name"])
    record["extra"].setdefault("selected_module", "00000000000000")
    # 把 selected_module 提到顶级以支持 {selected_module} 占位
    record["selected_module"] = record["extra"]["selected_module"]

    # 设备ID归一：MAC 形如 94a9902db4b0，未绑定设备用占位，统一 12 宽
    _dev = record["extra"].get("device_id")
    if not _dev:
        _dev = "------------"
    _dev = str(_dev)[:12].ljust(12, "-")
    record["extra"]["device_id"] = _dev
    record["device_id"] = _dev
    return record["message"]


def setup_logging(config=None):
    """按配置文件初始化 loguru；仅首次真正配置，后续直接返回。"""
    if config is None:
        check_config_file()
        # 先查缓存，避免在 async 上下文中重复 await load_config
        config = cache_manager.get(CacheType.CONFIG, "main_config")
        if config is None:
            # 缓存也没有（理论上不该发生），才走 asyncio.run
            config = asyncio.run(load_config())
    log_config = config["log"]
    global _logger_initialized

    # 第一次初始化时配置日志
    if not _logger_initialized:
        logger.configure(
            extra={
                "selected_module": log_config.get("selected_module", "00000000000000"),
            }
        )

        log_format = log_config.get(
            "log_format",
            "<green>{time:YYMMDD HH:mm:ss}</green>[{version}_{extra[selected_module]}][<light-blue>{extra[tag]}</light-blue>][<cyan>{device_id}</cyan>]-<level>{level}</level>-<light-green>{message}</light-green>",
        )
        log_format_file = log_config.get(
            "log_format_file",
            "{time:YYYY-MM-DD HH:mm:ss} - {version}_{extra[selected_module]} - {name} - {level} - {extra[tag]} - {device_id} - {message}",
        )
        log_format = log_format.replace("{version}", SERVER_VERSION)
        log_format_file = log_format_file.replace("{version}", SERVER_VERSION)

        log_level = log_config.get("log_level", "INFO")
        log_dir = log_config.get("log_dir", "tmp")
        log_file = log_config.get("log_file", "server.log")
        data_dir = log_config.get("data_dir", "data")

        os.makedirs(log_dir, exist_ok=True)
        os.makedirs(data_dir, exist_ok=True)

        # 配置日志输出
        logger.remove()

        # 输出到控制台
        logger.add(sys.stdout, format=log_format, level=log_level, filter=formatter)

        # 输出到文件 - 统一目录，按大小轮转
        log_file_path = os.path.join(log_dir, log_file)
        logger.add(
            log_file_path,
            format=log_format_file,
            level=log_level,
            filter=formatter,
            rotation="10 MB",  # 每个文件最大10MB
            retention="30 days",  # 保留30天
            compression=None,
            encoding="utf-8",
            enqueue=True,  # 异步安全
            backtrace=True,
            diagnose=True,
        )
        _logger_initialized = True  # 标记为已初始化

    return logger


def create_connection_logger(selected_module_str):
    """为某条连接创建绑定了模块字符串的 logger。"""
    return logger.bind(selected_module=selected_module_str)
