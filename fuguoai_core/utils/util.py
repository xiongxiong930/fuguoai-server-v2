"""复果AI 通用工具函数集。

按用途分四组：
  1. 网络/IP        — get_local_ip / get_ip_info
  2. 文本/配置      — remove_punctuation_and_length / check_model_key /
                      extract_json_from_string / filter_sensitive_info /
                      sanitize_tool_name / validate_mcp_endpoint /
                      normalize_device_id / get_system_error_response / get_vision_url
  3. 音频转码       — audio_to_data / audio_to_data_stream /
                      audio_bytes_to_data_stream / opus_datas_to_wav_bytes /
                      check_ffmpeg_installed
  4. 配置热更判定    — check_vad_update / check_asr_update

约定：
  - 音频统一为 16bit 小端单声道；opus 帧长 60ms。
  - 流式接口 (*_stream) 通过 callback 逐帧回吐，绝不在内存里攒整段。
  - 设备 ID 一律先过 normalize_device_id 再入库/查库。
"""

import re
import os
import json
import copy
import wave
import socket
import asyncio
import subprocess
from io import BytesIO
from typing import Any, Callable

import requests
import numpy as np
import opuslib_next
from pydub import AudioSegment

from fuguoai_core.utils import p3

TAG = __name__

# ---- 音频常量（全项目统一，勿在调用侧另行硬编码） ----
FRAME_MS = 60                 # opus 帧长
DEFAULT_SAMPLE_RATE = 16000   # 默认采样率
SAMPLE_WIDTH = 2              # 16bit


# ============================================================
# 1. 网络 / IP
# ============================================================

def get_local_ip() -> str:
    """取本机对外网卡 IP；失败时回退 127.0.0.1。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


_IP_RE = re.compile(
    r"^(\d{1,3}\.){3}\d{1,3}$|^([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}$"
)


def _is_private_ip(ip_addr: str) -> bool:
    """判断是否内网/保留地址（IPv4 + 常见 IPv6 形态）。"""
    try:
        if not _IP_RE.match(ip_addr):
            return False
        if "." in ip_addr:
            a, b = (int(x) for x in ip_addr.split(".")[:2])
            return (
                a == 10
                or (a == 172 and 16 <= b <= 31)
                or (a == 192 and b == 168)
                or (a == 169 and b == 254)
                or ip_addr == "127.0.0.1"
            )
        low = ip_addr.lower()
        return low == "::1" or low.startswith(("fc00:", "fd00:", "fe80:"))
    except (ValueError, IndexError):
        return False


def get_ip_info(ip_addr: str, logger) -> dict:
    """查 IP 归属地（带缓存）。内网地址按空 IP 查询（即查出口 IP）。"""
    try:
        from fuguoai_core.utils.cache.manager import cache_manager, CacheType

        cached = cache_manager.get(CacheType.IP_INFO, ip_addr)
        if cached is not None:
            return cached

        query_ip = "" if _is_private_ip(ip_addr) else ip_addr
        try:
            resp = requests.get(
                f"https://whois.pconline.com.cn/ipJson.jsp?json=true&ip={query_ip}",
                timeout=5,
            ).json()
        except requests.exceptions.Timeout:
            # 演进点：外部 IP 归属地服务挂起时必须有硬超时。
            # 该函数在天气预热路径里跑在事件循环上（get_weather → call_soon_threadsafe），
            # 若无限等待会将整个单事件循环冻结（所有设备 WS / 对讲 relay / events 卡死）。
            logger.bind(tag=TAG).warning("查询 IP 归属地超时，降级返回空")
            return {}
        info = {"city": resp.get("city")}
        cache_manager.set(CacheType.IP_INFO, ip_addr, info)
        return info
    except Exception as e:
        logger.bind(tag=TAG).error(f"查询 IP 归属地失败: {e}")
        return {}


# ============================================================
# 2. 文本 / 配置
# ============================================================

_FULL_WIDTH_PUNCT = "！＂＃＄％＆＇（）＊＋，－。／：；＜＝＞？＠［＼］＾＿｀｛｜｝～"
_HALF_WIDTH_PUNCT = r'!"#$%&\'()*+,-./:;<=>?@[\]^_`{|}~'
_STRIP_CHARS = set(_FULL_WIDTH_PUNCT) | set(_HALF_WIDTH_PUNCT) | {" ", "　"}


def remove_punctuation_and_length(text: str):
    """去标点与空格后返回 (有效长度, 净文本)。

    ASR 幻听常返回孤词 "Yeah"，视为无效输入。
    """
    result = "".join(ch for ch in text if ch not in _STRIP_CHARS)
    if result == "Yeah":
        return 0, ""
    return len(result), result


def check_model_key(model_type: str, model_key: str):
    """API key 仍是模板占位（含"你"字样）时返回错误提示，正常返回 None。"""
    if "你" in model_key:
        return f"配置错误: {model_type} 的 API key 未设置,当前值为: {model_key}"
    return None


def extract_json_from_string(input_string: str):
    """从任意文本中提取第一个 {...} JSON 片段；无则返回 None。"""
    match = re.search(r"(\{.*\})", input_string, re.DOTALL)
    return match.group(1) if match else None


_SENSITIVE_KEY_WORDS = (
    "api_key",
    "personal_access_token",
    "access_token",
    "token",
    "secret",
    "access_key_secret",
    "secret_key",
)


def filter_sensitive_info(config: dict) -> dict:
    """深拷贝配置并把敏感字段值打码为 ***（含嵌套 dict/list/JSON 字符串）。"""

    def _walk(d: dict) -> dict:
        out = {}
        for k, v in d.items():
            if any(w in k.lower() for w in _SENSITIVE_KEY_WORDS):
                out[k] = "***"
            elif isinstance(v, dict):
                out[k] = _walk(v)
            elif isinstance(v, list):
                out[k] = [_walk(i) if isinstance(i, dict) else i for i in v]
            elif isinstance(v, str):
                try:
                    inner = json.loads(v)
                    out[k] = (
                        json.dumps(_walk(inner), ensure_ascii=False)
                        if isinstance(inner, dict)
                        else v
                    )
                except (json.JSONDecodeError, TypeError):
                    out[k] = v
            else:
                out[k] = v
        return out

    return _walk(copy.deepcopy(config))


def get_vision_url(config: dict) -> str:
    """取视觉解释接口 URL；配置仍是模板占位时按本机 IP 自动拼装。"""
    server_config = config["server"]
    vision_explain = server_config.get("vision_explain", "")
    if "你的" in vision_explain:
        port = int(server_config.get("http_port", 8003))
        vision_explain = f"http://{get_local_ip()}:{port}/mcp/vision/explain"
    return vision_explain


_IMAGE_MAGIC = (
    b"\xff\xd8\xff",        # JPEG
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"GIF87a",              # GIF
    b"GIF89a",              # GIF
    b"BM",                  # BMP
    b"II*\x00",             # TIFF (LE)
    b"MM\x00*",             # TIFF (BE)
    b"RIFF",                # WEBP
)


def is_valid_image_file(file_data: bytes) -> bool:
    """按文件头魔数判断是否为常见图片格式。"""
    return file_data.startswith(_IMAGE_MAGIC)


def sanitize_tool_name(name: str) -> str:
    """工具名净化为 OpenAI function 兼容形式（保留中文/字母/数字/下划线/连字符）。"""
    return re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]", "_", name)


def validate_mcp_endpoint(mcp_endpoint: str) -> bool:
    """MCP 接入点须为 ws(s) 地址、路径含 /mcp/、且不得夹带 key/call 字样。"""
    low = mcp_endpoint.lower()
    return (
        mcp_endpoint.startswith("ws")
        and "/mcp/" in mcp_endpoint
        and "key" not in low
        and "call" not in low
    )


def normalize_device_id(raw):
    """设备 ID 归一化：统一 WS 固件(MAC 带冒号小写)与小程序侧(无冒号大写)等形态。

    规范形式 = 小写 + 去除冒号与连字符，例如：
        "88:56:a6:fb:0f:f4" -> "8856a6fb0ff4"
        "8856A6FB0FF4"      -> "8856a6fb0ff4"
        "TEST-PRESET-DEV"   -> "testpresetdev"
    同一物理设备的不同来源由此收敛为同一 key，避免记忆/配置因 ID 形态分裂而互查不到。
    仅做形态归一，不改变空值/None 语义。
    """
    if not raw:
        return raw
    return str(raw).strip().lower().replace(":", "").replace("-", "")


def get_system_error_response(config: dict) -> str:
    """系统异常时的兜底话术（可由配置覆盖）。"""
    return config.get("system_error_response", "主人，复果现在有点忙，我们稍后再试吧。")


# ============================================================
# 3. 音频转码
# ============================================================

def check_ffmpeg_installed() -> bool:
    """确认 ffmpeg 可执行；不可用时抛 ValueError 并附排查建议。"""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        if "ffmpeg version" in (result.stdout + result.stderr).lower():
            return True
        raise ValueError("未检测到有效的 ffmpeg 版本输出。")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        stderr_output = (
            (e.stderr or "").strip()
            if isinstance(e, subprocess.CalledProcessError)
            else str(e).strip()
        )
        msg = [
            "检测到 ffmpeg 无法正常运行。",
            "请确认部署环境已安装 ffmpeg（容器镜像默认自带；本地环境可用",
            "conda install -c conda-forge ffmpeg 或系统包管理器安装）。",
        ]
        if "libiconv.so.2" in stderr_output:
            msg.append("缺少依赖库 libiconv.so.2：conda install -c conda-forge libiconv")
        elif stderr_output:
            msg.append(f"错误详情：{stderr_output}")
        raise ValueError("\n".join(msg)) from e


def _load_pcm(source, file_type: str, sample_rate: int) -> bytes:
    """用 pydub/ffmpeg 把任意音频源统一解码为 16bit 单声道 PCM。

    -nostdin：防止 ffmpeg 子进程读 stdin 阻塞。
    """
    audio = AudioSegment.from_file(source, format=file_type or None, parameters=["-nostdin"])
    audio = (
        audio.set_channels(1)
        .set_frame_rate(sample_rate)
        .set_sample_width(SAMPLE_WIDTH)
    )
    return audio.raw_data


def _iter_pcm_frames(raw_data: bytes, sample_rate: int):
    """按 FRAME_MS 帧长切 PCM，末帧补零。yield (chunk, frame_size, is_last)。"""
    frame_size = int(sample_rate * FRAME_MS / 1000)
    step = frame_size * SAMPLE_WIDTH
    total = len(raw_data)
    for i in range(0, total, step):
        chunk = raw_data[i : i + step]
        if len(chunk) < step:
            chunk += b"\x00" * (step - len(chunk))
        yield chunk, frame_size, (i + step >= total)


def _pcm_to_data_stream(
    raw_data: bytes,
    is_opus: bool = True,
    callback: Callable[[Any], Any] = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    opus_encoder=None,
) -> None:
    """PCM 流式编码为 opus（或原样回吐 PCM），逐帧经 callback 输出。

    opus_encoder 传入 OpusEncoderUtils 时复用其编码状态（TTS 流式场景必须，
    否则帧间状态断裂产生咔哒声）；不传则用临时编码器（独立短音频场景）。
    """
    temp_encoder = None
    if is_opus and opus_encoder is None:
        temp_encoder = opuslib_next.Encoder(
            sample_rate, 1, opuslib_next.APPLICATION_AUDIO
        )

    for chunk, frame_size, is_last in _iter_pcm_frames(raw_data, sample_rate):
        if not is_opus:
            callback(chunk if isinstance(chunk, bytes) else bytes(chunk))
        elif temp_encoder is not None:
            np_frame = np.frombuffer(chunk, dtype=np.int16)
            callback(temp_encoder.encode(np_frame.tobytes(), frame_size))
        else:
            opus_encoder.encode_pcm_to_opus_stream(
                chunk, end_of_stream=is_last, callback=callback
            )


def audio_to_data_stream(
    audio_file_path,
    is_opus: bool = True,
    callback: Callable[[Any], Any] = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    opus_encoder=None,
) -> None:
    """音频文件 → 流式 opus/pcm 帧（经 callback 逐帧输出）。"""
    file_type = os.path.splitext(audio_file_path)[1].lstrip(".")
    raw_data = _load_pcm(audio_file_path, file_type, sample_rate)
    _pcm_to_data_stream(raw_data, is_opus, callback, sample_rate, opus_encoder)


def audio_bytes_to_data_stream(
    audio_bytes: bytes,
    file_type: str,
    is_opus: bool,
    callback: Callable[[Any], Any],
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    opus_encoder=None,
) -> None:
    """音频二进制 → 流式 opus/pcm 帧。支持 wav/mp3/p3（p3 走专用解码）。"""
    if file_type == "p3":
        return p3.decode_opus_from_bytes_stream(audio_bytes, callback)
    raw_data = _load_pcm(BytesIO(audio_bytes), file_type, sample_rate)
    _pcm_to_data_stream(raw_data, is_opus, callback, sample_rate, opus_encoder)


async def audio_to_data(
    audio_file_path: str, is_opus: bool = True, use_cache: bool = True
) -> list[bytes]:
    """音频文件 → opus/pcm 帧列表（一次性返回，带 10 分钟缓存）。

    解码在线程池执行，不阻塞事件循环。
    """
    from fuguoai_core.utils.cache.manager import cache_manager
    from fuguoai_core.utils.cache.config import CacheType

    cache_key = f"{audio_file_path}:{is_opus}"
    if use_cache:
        cached = cache_manager.get(CacheType.AUDIO_DATA, cache_key)
        if cached is not None:
            return cached

    def _decode() -> list[bytes]:
        file_type = os.path.splitext(audio_file_path)[1].lstrip(".")
        raw_data = _load_pcm(audio_file_path, file_type, DEFAULT_SAMPLE_RATE)
        encoder = opuslib_next.Encoder(
            DEFAULT_SAMPLE_RATE, 1, opuslib_next.APPLICATION_AUDIO
        )
        frames = []
        for chunk, frame_size, _ in _iter_pcm_frames(raw_data, DEFAULT_SAMPLE_RATE):
            if is_opus:
                np_frame = np.frombuffer(chunk, dtype=np.int16)
                frames.append(encoder.encode(np_frame.tobytes(), frame_size))
            else:
                frames.append(chunk if isinstance(chunk, bytes) else bytes(chunk))
        return frames

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _decode)

    if use_cache:
        cache_manager.set(CacheType.AUDIO_DATA, cache_key, result)
    return result


def opus_datas_to_wav_bytes(
    opus_datas, sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = 1
) -> bytes:
    """opus 帧列表 → wav 字节流（16bit）。"""
    decoder = opuslib_next.Decoder(sample_rate, channels)
    frame_size = int(sample_rate * FRAME_MS / 1000)
    pcm_bytes = b"".join(decoder.decode(f, frame_size) for f in opus_datas)

    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


# ============================================================
# 4. 配置热更判定
# ============================================================

def _module_type(config: dict, section: str, module_name: str) -> str:
    """取某 section 下模块的实际 type（缺省时以模块名为 type）。"""
    block = config[section][module_name]
    return block.get("type", module_name) if isinstance(block, dict) else module_name


def check_vad_update(before_config: dict, new_config: dict) -> bool:
    """VAD 实际 type 变化才需要重建实例。"""
    selected = new_config.get("selected_module") or {}
    if selected.get("VAD") is None:
        return False
    return _module_type(
        before_config, "VAD", before_config["selected_module"]["VAD"]
    ) != _module_type(new_config, "VAD", selected["VAD"])


def check_asr_update(before_config: dict, new_config: dict) -> bool:
    """ASR 模块名或实际 type 变化都需要重建实例。"""
    selected = new_config.get("selected_module") or {}
    if selected.get("ASR") is None:
        return False
    current_name = before_config["selected_module"]["ASR"]
    new_name = selected["ASR"]
    if current_name != new_name:
        return True
    return _module_type(before_config, "ASR", current_name) != _module_type(
        new_config, "ASR", new_name
    )
