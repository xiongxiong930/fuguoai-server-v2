"""本地音乐播放工具（复果自有实现）。

LLM 通过 ``play_music`` 工具请求播放音乐。本模块在本地曲库里按歌名匹配，
匹配不上就随机挑一首，再把「播报语 + 文本 + 音频文件」依次塞进 TTS 队列。

对外契约：工具名 play_music、参数 schema、以及始终返回
``Action.RECORD / 指令已接收 / 正在为您播放音乐`` —— 这些都保持稳定。
对外被依赖的 ``initialize_music_handler(conn)`` 也保持原签名与返回结构。
"""

import os
import re
import time
import random
import difflib
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from fuguoai_core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType
from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import register_function, ToolType, ActionResponse, Action

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 曲库缓存（首次访问时按配置扫描目录填充）
MUSIC_CACHE = {}

play_music_function_desc = {
    "type": "function",
    "function": {
        "name": "play_music",
        "description": "当用户要求播放音乐、歌曲时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": "歌曲名称，如果用户没有指定具体歌名则为'random', 明确指定的时返回音乐的名字 示例: ```用户:播放两只老虎\n参数：两只老虎``` ```用户:播放音乐 \n参数：random ```",
                }
            },
            "required": ["song_name"],
        },
    },
}


def _pull_song(text: str):
    """从「播放音乐 xxx」里抠出具体歌名。"""
    marker = "播放音乐"
    if marker not in text:
        return None
    pieces = text.split(marker)
    return pieces[1].strip() if len(pieces) > 1 else None


def _best_match(query: str, candidates: list):
    """在曲库里挑相似度最高且过阈值的那一首（0.4 以上才算认得）。"""
    winner, top = None, 0.0
    for name in candidates:
        score = difflib.SequenceMatcher(None, query, os.path.splitext(name)[0]).ratio()
        if score > top and score > 0.4:
            top, winner = score, name
    return winner


def _walk_library(root, extensions) -> tuple:
    """扫描目录，返回 (相对路径列表, 去扩展名文件名列表)。"""
    root_path = Path(root)
    paths, bare_names = [], []
    if not root_path.exists():
        return paths, bare_names
    for entry in root_path.rglob("*"):
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in extensions:
            continue
        rel = str(entry.relative_to(root_path))
        paths.append(rel)
        bare_names.append(os.path.splitext(rel)[0])
    return paths, bare_names


def initialize_music_handler(conn: "ConnectionHandler"):
    """按 plugins.play_music 配置初始化 MUSIC_CACHE（只初始化一次）。

    返回整张缓存表，外部（意图识别模块）会取其中的 ``music_file_names``。
    """
    global MUSIC_CACHE
    if MUSIC_CACHE:
        return MUSIC_CACHE

    plugin_cfg = conn.config.get("plugins", {}).get("play_music", {})
    if plugin_cfg:
        MUSIC_CACHE["music_config"] = plugin_cfg
        MUSIC_CACHE["music_dir"] = os.path.abspath(plugin_cfg.get("music_dir", "./music"))
        MUSIC_CACHE["music_ext"] = plugin_cfg.get("music_ext", (".mp3", ".wav", ".p3"))
        MUSIC_CACHE["refresh_time"] = plugin_cfg.get("refresh_time", 60)
    else:
        MUSIC_CACHE["music_dir"] = os.path.abspath("./music")
        MUSIC_CACHE["music_ext"] = (".mp3", ".wav", ".p3")
        MUSIC_CACHE["refresh_time"] = 60

    MUSIC_CACHE["music_files"], MUSIC_CACHE["music_file_names"] = _walk_library(
        MUSIC_CACHE["music_dir"], MUSIC_CACHE["music_ext"]
    )
    MUSIC_CACHE["scan_time"] = time.time()
    return MUSIC_CACHE


_GREETINGS = [
    "正在为您播放，《{name}》",
    "请欣赏歌曲，《{name}》",
    "即将为您播放，《{name}》",
    "现在为您带来，《{name}》",
    "让我们一起聆听，《{name}》",
    "接下来请欣赏，《{name}》",
    "此刻为您献上，《{name}》",
]


def _random_line(song_file: str) -> str:
    """挑一句随机播报语（先去掉文件扩展名）。"""
    return random.choice(_GREETINGS).format(name=os.path.splitext(song_file)[0])


async def _stream_track(conn: "ConnectionHandler", specific_file=None):
    """把本地音乐文件塞进 TTS 队列：先播报语，再推文本，最后推音频文件。"""
    global MUSIC_CACHE
    library_dir = MUSIC_CACHE["music_dir"]
    if not os.path.exists(library_dir):
        conn.logger.bind(tag=TAG).error(f"音乐目录不存在: {library_dir}")
        return

    if specific_file:
        picked, track_path = specific_file, os.path.join(library_dir, specific_file)
    else:
        if not MUSIC_CACHE["music_files"]:
            conn.logger.bind(tag=TAG).error("未找到MP3音乐文件")
            return
        picked = random.choice(MUSIC_CACHE["music_files"])
        track_path = os.path.join(library_dir, picked)

    if not os.path.exists(track_path):
        conn.logger.bind(tag=TAG).error(f"选定的音乐文件不存在: {track_path}")
        return

    line = _random_line(picked)
    conn.tts.store_tts_text(conn.sentence_id, line)

    envelope = conn.tts.tts_text_queue
    if conn.intent_type == "intent_llm":
        envelope.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
    envelope.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT,
            content_detail=line,
        )
    )
    envelope.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.FILE,
            content_file=track_path,
        )
    )
    if conn.intent_type == "intent_llm":
        envelope.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )


async def _dispatch_music(conn: "ConnectionHandler", text: str):
    """处理一条播放指令：优先按歌名精确匹配，否则随机放。"""
    global MUSIC_CACHE
    initialize_music_handler(conn)

    cleaned = re.sub(r"[^\w\s]", "", text).strip()
    conn.logger.bind(tag=TAG).debug(f"检查是否是音乐命令: {cleaned}")

    if os.path.exists(MUSIC_CACHE["music_dir"]):
        if time.time() - MUSIC_CACHE["scan_time"] > MUSIC_CACHE["refresh_time"]:
            MUSIC_CACHE["music_files"], MUSIC_CACHE["music_file_names"] = _walk_library(
                MUSIC_CACHE["music_dir"], MUSIC_CACHE["music_ext"]
            )
            MUSIC_CACHE["scan_time"] = time.time()

        requested = _pull_song(cleaned)
        if requested:
            hit = _best_match(requested, MUSIC_CACHE["music_files"])
            if hit:
                conn.logger.bind(tag=TAG).info(f"找到最匹配的歌曲: {hit}")
                await _stream_track(conn, specific_file=hit)
                return True

    await _stream_track(conn)
    return True


@register_function("play_music", play_music_function_desc, ToolType.SYSTEM_CTL)
async def play_music(conn: "ConnectionHandler", song_name: str):
    """按 LLM 给的歌名（或 random）触发本地音乐播放。"""
    try:
        intent = f"播放音乐 {song_name}" if song_name != "random" else "随机播放音乐"
        await _dispatch_music(conn, intent)
        return ActionResponse(
            action=Action.RECORD, result="指令已接收", response="正在为您播放音乐"
        )
    except Exception as exc:
        conn.logger.bind(tag=TAG).error(f"处理音乐意图错误: {exc}")
        return ActionResponse(
            action=Action.RESPONSE, result=str(exc), response="播放音乐时出错了"
        )
