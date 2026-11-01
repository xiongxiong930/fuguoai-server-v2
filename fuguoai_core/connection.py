"""复果连接处理器（ConnectionHandler）：单条设备 WebSocket 生命周期的总编排。

本模块由复果独立实现，负责：
- hello 握手入口、设备标识解析、用量计量与在线状态登记；
- 上行音频（opus）解码、VAD/ASR 喂帧、服务端 AEC 消回声与 barge-in 真人插话裁决；
- TTS 下行文本/音频流控与首包延迟可观测；
- 对话编排（chat 主循环、工具路由、记忆/人格/家庭知识库注入、离线兜底、对话自评估）；
- 连接看门狗（静默存活探测）、畸形连接防护、AEC 缓存清理、重启自愈。

线协议字段与 xiaozhi-esp32 固件保持兼容（设备侧握手/welcome/音频帧/MQTT topic 不变），
以便现有测试设备无需改固件即可接入；固件侧协议随后端独立演进。
"""

import os
import sys
import copy
import json
import re
import uuid
import time
import queue
import asyncio
import threading
import traceback
import subprocess
import websockets
import opuslib_next
import numpy as np

from fuguoai_core.utils.util import (
    extract_json_from_string,
    check_vad_update,
    check_asr_update,
    filter_sensitive_info,
    normalize_device_id,
)

# 低频/场景类工具按需下发门控（对话算法优化 2026-09-10）：
# 这类工具仅当对话上下文命中触发词、或近期已调用过时才发给 LLM，
# 以缩小每轮固定 schema 体积。核心工具（设备控制/退出/播放音乐等）始终下发。
_TOOL_GATE = {
    "web_search": ["搜索", "查一下", "查一查", "上网搜", "百度", "谷歌", "搜一下", "搜"],
    "get_news_from_newsnow": ["新闻", "资讯", "头条", "要闻", "最近发生"],
    "get_lunar": ["农历", "阴历", "黄历", "节气", "几月几号星期", "星期几"],
    "get_speaker_volume": ["音量", "声音", "响", "小声", "大声"],
    "speaker_setvolume": ["音量", "声音", "响一点", "小声", "大声", "调声音"],
    "get_screen_theme": ["屏幕", "主题", "壁纸"],
    "get_screen_brightness": ["屏幕", "亮度", "暗", "亮"],
    "screen_settheme": ["屏幕", "主题", "壁纸", "换壁纸"],
    "screen_setbrightness": ["屏幕", "亮度", "调亮", "调暗", "暗一点", "亮一点"],
    "media_openplayer": ["音乐", "歌", "播放", "放歌", "听歌"],
    "media_openalbum": ["相册", "照片", "图库"],
    "media_playsdmusic": ["音乐", "歌", "播放", "听歌"],
    "media_showsdphoto": ["照片", "图片", "相册", "看照片"],
    "media_next": ["下一首", "换一首", "切歌"],
    "media_prev": ["上一首", "切回"],
    "media_exit": ["停止播放", "暂停", "不播了", "关掉音乐"],
}

# 前缀门控：整类工具按触发词下发。devicecontrol 家族(20+ 个 IoT 工具)仅在设备指令时
# 下发，避免空闲闲聊时每轮固定开销（实测约占输入 token 60%）。触发词取宽，宁可多发。
_TOOL_GATE_PREFIX = {
    "devicecontrol": [
        "灯", "开", "关", "亮", "暗", "设备", "模式", "氛围", "颜色",
        "定时", "任务", "闹钟", "提醒", "对讲", "播放", "停",
        "亮度", "空调", "智能", "家电", "音",
    ],
    "get_devicecontrol": ["灯", "设备", "状态", "模式", "氛围", "亮度", "开着", "亮不亮", "多少"],
}
from typing import Dict, Any
from collections import deque
from fuguoai_core.utils.modules_initialize import (
    initialize_modules,
    initialize_tts,
    initialize_asr,
)
from fuguoai_core.handle.reportHandle import report, enqueue_tool_report
from fuguoai_core.providers.tts.default import DefaultTTS
from concurrent.futures import ThreadPoolExecutor
from fuguoai_core.utils.dialogue import Message, Dialogue
from fuguoai_core.providers.asr.dto.dto import InterfaceType
from fuguoai_core.handle.textHandle import handleTextMessage
from fuguoai_core.providers.tools.unified_tool_handler import UnifiedToolHandler
from fuguoai_plugins.loadplugins import auto_import_modules
from fuguoai_plugins.register import Action, ActionResponse
from fuguoai_core.auth import AuthenticationError
from fuguoai_config.config_loader import get_private_config_from_api
from fuguoai_core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType
from fuguoai_config.logger import setup_logging, build_module_string, create_connection_logger
from fuguoai_config.manage_api_client import DeviceNotFoundException, DeviceBindException, generate_and_save_chat_title
from fuguoai_core.utils.prompt_manager import PromptManager
from fuguoai_core.utils.voiceprint_provider import VoiceprintProvider, get_voiceprint_provider
from fuguoai_core.utils.util import get_system_error_response
from fuguoai_core.utils import textUtils
from fuguoai_core.utils.metrics import METRICS
from fuguoai_core.utils.usage_store import (
    get_usage_store,
    set_current_device_session,
    reset_current_device_session,
)
# 对话自评估 + A/B 测试 + 离线兜底（自主演进维度④）
from fuguoai_core.utils.offline_fallback import build_offline_fallback
from fuguoai_core.utils.dialogue_eval import get_dialogue_eval
from fuguoai_core.utils.ab_test import get_ab_test
from fuguoai_core.utils.persona import get_persona_store
from fuguoai_core.utils.family_rag import get_family_rag
TAG = __name__

auto_import_modules("fuguoai_plugins.functions")

# ==========================================================================
# 进程级共享有界 chat executor
# --------------------------------------------------------------------------
# 旧实现每连接新建 ThreadPoolExecutor(10)：n 台设备即 10n 个线程，线程数随连接数
# 无界增长（100 台设备=1000 线程）。改为进程级单例有界池（默认 32，可用
# FUGUOAI_CHAT_EXECUTOR_WORKERS 调节），线程名前缀 chat 便于 py-spy 定位。
# 提交点不感知差异；close 不得 shutdown 共享池。
# ==========================================================================
_SHARED_CHAT_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.environ.get("FUGUOAI_CHAT_EXECUTOR_WORKERS", "32")),
    thread_name_prefix="chat",
)


class TTSException(RuntimeError):
    pass


# direct_answer 虚拟工具：将"调不调工具"的二选一变为"调哪个"的多选，防止小模型误触发真实工具
DIRECT_ANSWER_TOOL = {
    "type": "function",
    "function": {
        "name": "direct_answer",
        "description": "当用户的请求不匹配其他任何工具时，可用此选项直接回复。将回复内容写在response参数里。",
        "parameters": {
            "type": "object",
            "properties": {
                "response": {
                    "type": "string",
                    "description": "你回复用户的完整内容",
                },
            },
            "required": ["response"],
        },
    },
}


# 记忆保存冷却期（按 device_id），防固件闪断频繁覆盖长记忆
MEMORY_SAVE_COOLDOWN: Dict[str, float] = {}
# 记忆总结冷却（秒）。每轮对话结束触发 save_memory → 调一次 LLM 总结；
# 对讲密集调试期高频触发是混元 token 浪费主因之一。默认 300s（=5 分钟最多 1 次/设备）。
MEMORY_SAVE_COOLDOWN_SEC = 300


class ConnectionHandler:
    def __init__(
            self,
            config: Dict[str, Any],
            _vad,
            _asr,
            _llm,
            _memory,
            _intent,
            server=None,
    ):
        self.common_config = config
        self.config = copy.deepcopy(config)
        self.session_id = str(uuid.uuid4())
        # 连接实例身份：session_id 由固件在 hello 里带上，实测设备会对主对话 WS 与
        # intercom 音频 WS 复用同一值，导致 connection_registry 无法区分新旧连接。
        # conn_uid 每条 WS 连接唯一（纯服务端内部，不上协议、固件无感），作为注销/接管判据。
        self.conn_uid = uuid.uuid4().hex
        self.logger = setup_logging()
        self.server = server

        self.need_bind = False
        self.bind_completed_event = asyncio.Event()
        self.bind_code = None
        self.last_bind_prompt_time = 0
        self.bind_prompt_interval = 60

        # 畸形连接防护：不改协议、不进对话/断句/TTS 路径，仅在入口做计数与主动关闭。
        # 场景：固件裸发 JSON-RPC(无 type 信封)或非法文本被丢弃后连接不关，配合 keepalive
        # 可挂死数十分钟。对「未知类型/非 JSON」消息做累计 + 滑窗限流，超阈值主动踢除。
        _mg = (self.config.get("malformed_guard", {}) or {})
        self._mg_enable = bool(_mg.get("enabled", True))
        self._mg_max_total = int(_mg.get("max_total", 30))
        self._mg_window_sec = float(_mg.get("window_sec", 10))
        self._mg_window_max = int(_mg.get("window_max", 10))
        self._mg_handshake_sec = float(_mg.get("handshake_sec", 30))
        self._malformed_total = 0
        self._malformed_ts = deque(maxlen=128)
        self._hello_received = False
        self._first_msg_ts = 0.0

        self.read_config_from_api = self.config.get("read_config_from_api", False)

        self.websocket: websockets.ServerConnection | None = None
        self.headers = None
        # WebSocket 发送串行锁：websockets 的 send 非并发安全，必须串行化。
        self._send_lock = asyncio.Lock()
        self.device_id = None
        self.fw_version = None
        self.client_ip = None
        self.prompt = None
        self.welcome_msg = None
        self.max_output_size = 0
        self.chat_history_conf = 0
        self.audio_format = "opus"
        self.sample_rate = 24000

        self.client_abort = False
        self.client_is_speaking = False
        self._usage_chars = 0
        self._usage_turns = 0
        self._session_start_ts = 0.0
        # 首包延迟观测：记录本轮首帧音频发出的耗时
        self._first_audio_mark = None
        self._first_audio_recorded = False
        self._llm_first_token_recorded = False
        self.client_listen_mode = "auto"
        self.client_aec = False
        # 全双工阶段2：AEC 消回声后是否主动打断 TTS（插话打断）。默认开启，但带残留回声
        # 守卫，仅当本帧 AEC 确实消掉回声（真人插话）时才允许 abort，杜绝被自身 TTS 回声自打断。
        self.client_aec_bargein = True
        self._aec_last_effective = False
        self._raw_mic_rms = 0.0
        self._echo_baseline_rms = 0.0
        self._baseline_ready = False
        self._aec_eff_streak = 0
        # 全双工 AEC 参考帧缓存（直连 WS 路径也需初始化，不依赖 MQTT 路径）
        self.aec_audio_cache = {}
        self.aec_audio_cache_time = {}

        self.loop = None
        self.stop_event = threading.Event()
        # 共享有界池（进程级单例），不再每连接建 10 线程
        self.executor = _SHARED_CHAT_EXECUTOR

        self.report_queue = queue.Queue()
        self.report_thread = None
        self.report_asr_enable = self.read_config_from_api
        self.report_tts_enable = self.read_config_from_api

        self.vad = None
        self.asr = None
        self.tts = None
        self._asr = _asr
        self._vad = _vad
        self.llm = _llm
        self.memory = _memory
        self.intent = _intent

        self.voiceprint_provider = None

        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_voice_window = deque(maxlen=5)
        self.first_activity_time = 0.0
        self.last_activity_time = 0.0
        self.vad_last_voice_time = 0.0
        self.client_voice_stop = False
        self.last_is_voice = False

        self.asr_audio = []
        self.asr_audio_queue = queue.Queue()
        self.current_speaker = None

        self.dialogue = Dialogue()

        self.sentence_id = None
        self.tts_MessageText = ""

        self.iot_descriptors = {}
        self.func_handler = None

        self.cmd_exit = self.config["exit_commands"]
        # 闭嘴/停止说话指令（子串匹配）：命中后停播报且不搭话、保持连接。
        self.cmd_stop_talking = self.config.get(
            "stop_talking_commands",
            [
                "闭嘴", "别吵", "不要吵", "吵死", "下去", "安静点",
                "别说话", "停止说话", "别bb", "shut up", "闭嘴吧", "你闭嘴",
            ],
        )

        self.close_after_chat = False
        self.load_function_plugin = False
        self.intent_type = "nointent"

        self.timeout_seconds = (
                int(self.config.get("close_connection_no_voice_time", 120)) + 60
        )
        self.timeout_task = None

        self.features = None

        self.conn_from_mqtt_gateway = False

        self.prompt_manager = PromptManager(self.config)

        self.calling = False
        self.incoming_call = None

    async def handle_connection(self, ws: websockets.ServerConnection):
        try:
            # 获取运行中的事件循环（必须在异步上下文中）
            self.loop = asyncio.get_running_loop()

            self.headers = dict(ws.request.headers)
            real_ip = self.headers.get("x-real-ip") or self.headers.get(
                "x-forwarded-for"
            )
            if real_ip:
                self.client_ip = real_ip.split(",")[0].strip()
            else:
                self.client_ip = ws.remote_address[0]
            self.logger.bind(tag=TAG).info(
                f"{self.client_ip} conn - Headers: {self.headers}"
            )

            self.device_id = normalize_device_id(self.headers.get("device-id", None))
            # 设备 ID 打标：连接入口即绑定到 logger，使后续日志均带 device_id
            self.logger = self.logger.bind(device_id=self.device_id)

            # 设备级配置覆盖层（连接时拉取；失败回落全局，绝不中断连接）
            try:
                from fuguoai_core.utils.agent_config_overlay import apply_device_overlay
                apply_device_overlay(self.config, self.device_id)
            except Exception as _ov_err:
                self.logger.bind(tag=TAG).warning(
                    f"应用设备配置覆盖层异常(已忽略): {_ov_err}"
                )

            # 用量计量 — 会话开始
            if self.device_id:
                METRICS.inc(
                    "fuguoai_device_sessions_total",
                    labels={"device_id": self.device_id},
                    help_text="设备累计连接数",
                )
                get_usage_store().record_session_start(self.device_id, self.session_id)
                # 在线状态登记（旁路、非阻塞，绝不进入对话/TTS 路径）
                try:
                    from fuguoai_core.utils.connection_registry import get_connection_registry
                    reg = get_connection_registry()
                    reg.mark_online(
                        self.device_id, self.session_id, getattr(self, "client_ip", None),
                        conn_uid=self.conn_uid,
                    )
                    # 踢设备：注册关闭回调（惰性读取 self.websocket）。仅发关闭帧即可——
                    # 连接自身的 finally->_save_and_close 会走完整清理+落库 offline。
                    reg.register_kick(
                        self.device_id, self.session_id,
                        lambda: self.websocket.close(),
                        conn_uid=self.conn_uid,
                    )
                    reg.register_send(
                        self.device_id, self.session_id,
                        lambda: self,
                        conn_uid=self.conn_uid,
                    )
                    METRICS.inc_gauge("fuguoai_ws_connections_active", help_text="设备当前活跃WS连接数")
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"资源清理/上下线异常: {e}")
                # 在线状态持久化：落库 device_binding.online/last_seen（旁路，失败不影响连接）
                try:
                    from fuguoai_core.native.device_store import set_online_async
                    await set_online_async(self.device_id, 1)
                except Exception as _db_err:
                    self.logger.bind(tag=TAG).warning(f"在线状态落库失败(已忽略): {_db_err}")
            self._session_start_ts = time.time()

            self.websocket = ws

            # 检查是否来自 MQTT 连接
            request_path = ws.request.path
            self.conn_from_mqtt_gateway = request_path.endswith("?from=mqtt_gateway")
            if self.conn_from_mqtt_gateway:
                self.logger.bind(tag=TAG).info("连接来自:MQTT网关")

            self.first_activity_time = time.time() * 1000
            self.last_activity_time = time.time() * 1000

            # 启动超时检查任务
            self.timeout_task = self._watch_task(asyncio.create_task(self._check_timeout()), "check_timeout")
            # 启动 AEC 缓存清理任务
            self._aec_cache_cleanup_task = self._watch_task(asyncio.create_task(self._check_aec_cache_expiry()), "aec_cache_cleanup")

            self.welcome_msg = self.config["fuguoai"]
            self.welcome_msg["session_id"] = self.session_id

            self.sample_rate = self.welcome_msg["audio_params"]["sample_rate"]
            self.logger.bind(tag=TAG).info(f"配置输出音频采样率为: {self.sample_rate}")

            # 在后台初始化配置和组件（完全不阻塞主循环）
            self._watch_task(asyncio.create_task(self._background_initialize()), "background_initialize")

            try:
                async for message in self.websocket:
                    await self._route_message(message)
            except websockets.exceptions.ConnectionClosed as e:
                self.logger.bind(tag=TAG).info(
                    f"连接关闭(WS) rcode={getattr(e, 'rcode', None)} "
                    f"reason={getattr(e, 'reason', '')!r} "
                    f"server_initiated={getattr(self, '_server_close', False)}"
                )

        except AuthenticationError as e:
            self.logger.bind(tag=TAG).error(f"Authentication failed: {str(e)}")
            return
        except Exception as e:
            stack_trace = traceback.format_exc()
            self.logger.bind(tag=TAG).error(f"Connection error: {str(e)}-{stack_trace}")
            return
        finally:
            try:
                await self._save_and_close(ws)
            except Exception as final_error:
                self.logger.bind(tag=TAG).error(f"最终清理时出错: {final_error}")
                try:
                    await self.close(ws)
                except Exception as close_error:
                    self.logger.bind(tag=TAG).error(
                        f"强制关闭连接时出错: {close_error}"
                    )

    async def _save_and_close(self, ws):
        """保存记忆并关闭连接"""
        if self.device_id:
            get_usage_store().record_session_end(self.device_id, self.session_id)
            # 在线状态注销（仅当确属本连接，防多连接串扰）。注销前先取「是否已被新连接接管」
            # 快照——注销本身会 pop 掉 _online，若放到注销之后再判，读到的必然是 None。
            taken_over = False
            try:
                from fuguoai_core.utils.connection_registry import get_connection_registry
                reg = get_connection_registry()
                taken_over = reg.is_taken_over(
                    self.device_id, self.session_id, getattr(self, "conn_uid", None)
                )
                _uid = getattr(self, "conn_uid", None)
                reg.mark_offline(self.device_id, self.session_id, conn_uid=_uid)
                reg.unregister_kick(self.device_id, self.session_id, conn_uid=_uid)
                reg.unregister_send(self.device_id, self.session_id, conn_uid=_uid)
                METRICS.dec_gauge("fuguoai_ws_connections_active")
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"资源清理/上下线异常: {e}")
            # 对讲会话断连兜底：进入 grace（GRACE_SEC 内重连恢复，超时对端收 peer_left）。
            # 快速重连联动防串扰：固件断线后 3s 即重连并 hello(session=intercom)，而本条
            # 旧连接的 TCP 死链可能要 ~45s 才被发现。若设备已有更新活跃连接接管，旧连接
            # 的迟到断开绝不能把活跃会话误打入 grace。taken_over 已在上方注销前用 conn_uid 判定。
            if getattr(self, "intercom_mode", False):
                try:
                    if taken_over:
                        self.logger.bind(tag=TAG).info(
                            f"[intercom] 旧连接迟到断开，设备已有新连接接管，"
                            f"跳过 grace device={self.device_id}"
                        )
                    else:
                        from fuguoai_core.utils.intercom_registry import get_intercom_registry
                        get_intercom_registry().on_disconnect(self.device_id)
                except Exception as e:
                    self.logger.bind(tag=TAG).warning(f"[intercom] 断连清理异常(已忽略): {e}")
            # 在线状态持久化：落库 online=0（旁路，失败不影响连接清理）
            try:
                from fuguoai_core.native.device_store import set_online_async
                await set_online_async(self.device_id, 0)
            except Exception as _db_err:
                self.logger.bind(tag=TAG).warning(f"离线状态落库失败(已忽略): {_db_err}")
        try:
            # 守护线程1：独立生成标题（不依赖记忆模型）
            if self.session_id:
                def generate_title_task():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(
                            generate_and_save_chat_title(self.session_id)
                        )
                    except Exception as e:
                        self.logger.bind(tag=TAG).error(f"生成标题失败: {e}")
                    finally:
                        try:
                            loop.close()
                        except Exception as e:
                            self.logger.bind(tag=TAG).error(f"资源清理/上下线异常: {e}")

                threading.Thread(target=generate_title_task, daemon=True).start()

            # 守护线程2：走老流程记忆保存（仅记忆，不含标题）
            # 每设备记忆保存冷却期，防固件闪断频繁覆盖长记忆
            if self.memory:
                now_ts = time.time()
                last = MEMORY_SAVE_COOLDOWN.get(self.device_id or "", 0)
                _cd = int(self.config.get("Memory", {}).get("memory_save_cooldown_sec", MEMORY_SAVE_COOLDOWN_SEC))
                if now_ts - last >= _cd:
                    MEMORY_SAVE_COOLDOWN[self.device_id or ""] = now_ts

                    def save_memory_task():
                        try:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            loop.run_until_complete(
                                self.memory.save_memory(
                                    self.dialogue.dialogue, self.session_id, role_id=self.device_id
                                )
                            )
                        except Exception as e:
                            self.logger.bind(tag=TAG).error(f"保存记忆失败: {e}")
                        finally:
                            try:
                                loop.close()
                            except Exception as e:
                                self.logger.bind(tag=TAG).error(f"资源清理/上下线异常: {e}")

                    threading.Thread(target=save_memory_task, daemon=True).start()
                else:
                    self.logger.bind(tag=TAG).debug(
                        f"跳过记忆保存 (冷却中, elapsed={now_ts-last:.1f}s)"
                    )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"保存记忆失败: {e}")
        finally:
            # 立即关闭连接，不等待记忆保存完成
            try:
                await self.close(ws)
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"保存记忆后关闭连接失败: {close_error}"
                )

    async def _discard_message_with_bind_prompt(self):
        """丢弃消息并检查是否需要播放绑定提示"""
        current_time = time.time()
        if current_time - self.last_bind_prompt_time >= self.bind_prompt_interval:
            self.last_bind_prompt_time = current_time
            from fuguoai_core.handle.receiveAudioHandle import check_bind_device
            self._watch_task(asyncio.create_task(check_bind_device(self)), "check_bind_device")

    async def _route_message(self, message):
        """消息路由"""
        # 记录首条消息到达时间，用于握手超时判定（仅记录一次，不改变原有路由）
        if self._first_msg_ts == 0.0:
            self._first_msg_ts = time.time() * 1000

        # 检查是否已经获取到真实的绑定状态
        if not self.bind_completed_event.is_set():
            try:
                await asyncio.wait_for(self.bind_completed_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                await self._discard_message_with_bind_prompt()
                return

        if self.need_bind:
            await self._discard_message_with_bind_prompt()
            return

        if isinstance(message, str):
            # 用量计量 — 对话轮次
            if self.device_id and message.strip():
                self._usage_turns += 1
                self._usage_chars += len(message)
                METRICS.inc(
                    "fuguoai_conversation_turns_total",
                    labels={"device_id": self.device_id},
                    help_text="设备对话轮次数",
                )
                get_usage_store().record_turn(self.device_id, self.session_id)
            # LLM token 计量：在每轮 handleTextMessage 上下文标记 device/session
            _tok_ctx = set_current_device_session(self.device_id, self.session_id)
            try:
                await handleTextMessage(self, message)
            finally:
                reset_current_device_session(_tok_ctx)
        elif isinstance(message, bytes):
            # ── 对讲（Intercom）透传短路（契约 v1.6.0 Part B）──
            # 必须早于 vad/asr 判空、mic_ts_header 剥离、opus 解码：
            # intercom 连接不初始化 VAD/ASR，帧格式 [4B seq][opus] 原样含头转对端。
            # 设备对讲音频 WS 可能未带 session:intercom hello（持久化复用通道），
            # 此时 intercom_mode=False → 上行 opus 落入 VAD/ASR 判空被静默丢弃 →
            # relay _last_frame_ts 永不为设备更新 → watchdog 6s 判假在线 → kill 会话。
            # 加 is_in_session fallback 救上行。
            _should_relay = getattr(self, "intercom_mode", False)
            if not _should_relay:
                try:
                    from fuguoai_core.utils.intercom_registry import get_intercom_registry
                    _should_relay = get_intercom_registry().is_in_session(self.device_id)
                except Exception:
                    pass
            if _should_relay:
                if self.config.get("fuguoai", {}).get("intercom", {}).get(
                    "intercom_debug_log", False
                ):
                    try:
                        c = getattr(self, "_intercom_bin", None)
                        if c is None:
                            c = {"n": 0, "t": time.time(), "tot": 0}
                            self._intercom_bin = c
                        c["n"] += 1
                        c["tot"] += len(message)
                        now = time.time()
                        if c["n"] <= 3 or now - c["t"] >= 2.0:
                            self.logger.bind(tag=TAG).info(
                                f"[intercom][debug] recv binary from {self.device_id} "
                                f"len={len(message)} (本窗口{c['n']}帧/{c['tot']}B)"
                            )
                            c["n"] = 0
                            c["tot"] = 0
                            c["t"] = now
                    except Exception:
                        pass
                try:
                    from fuguoai_core.utils.intercom_registry import get_intercom_registry
                    await get_intercom_registry().relay(self.device_id, message)
                except Exception as e:
                    self.logger.bind(tag=TAG).warning(f"[intercom] 透传失败(已忽略): {e}")
                return

            if self.vad is None or self.asr is None:
                return

            if self.conn_from_mqtt_gateway and len(message) >= 16:
                handled = await self._process_mqtt_audio_message(message)
                if handled:
                    return

            # 入口处直接解码 PCM，避免 VAD 和 ASR 重复解码
            # 麦流二进制帧前置 8 字节小端 uint64 同源 ts(ms)；旧固件走原路径。
            mic_fw_ts = 0
            audio_msg = message
            if getattr(self, "mic_ts_header", False) and len(message) >= 8:
                try:
                    mic_fw_ts = int.from_bytes(message[:8], "little")
                    audio_msg = message[8:]
                except Exception:
                    mic_fw_ts = 0
                    audio_msg = message
            pcm_frame = self._decode_opus_packet(audio_msg)
            if pcm_frame:
                # 全双工(直连 WS)：服务端 AEC 消回声。对齐统一用服务端时钟。
                if self.client_aec:
                    local_ts = int(time.time() * 1000)
                    pcm_frame = self._apply_aec(local_ts, pcm_frame)
                self.asr_audio_queue.put(pcm_frame)
                self._last_mic_fw_ts = mic_fw_ts

    async def _process_mqtt_audio_message(self, message):
        """处理来自 MQTT 网关的音频消息，解析 16 字节头部并提取音频数据，在入队前做 AEC。"""
        try:
            timestamp = int.from_bytes(message[8:12], "big")
            audio_data = message[16:]
            pcm_frame = self._decode_opus_packet(audio_data)
            if not pcm_frame:
                return True
            if timestamp > 0 and self.client_aec:
                pcm_frame = self._apply_aec(timestamp, pcm_frame)
            self.asr_audio_queue.put(pcm_frame)
            return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"解析WebSocket音频包失败: {e}")
        return False

    def log_power_spectrum_corr(self, audio1, audio2):
        """对数功率谱相关性（频域幅度谱不受相位影响，稳定在 0.97+）。"""
        window = np.hanning(len(audio1))
        fft1 = np.fft.rfft(audio1 * window)
        fft2 = np.fft.rfft(audio2 * window)
        psd1 = np.abs(fft1) ** 2
        psd2 = np.abs(fft2) ** 2
        log1 = 10 * np.log10(psd1 + 1e-8)
        log2 = 10 * np.log10(psd2 + 1e-8)
        P_xy = np.dot(log1, log2)
        P_xx = np.dot(log1, log1)
        P_yy = np.dot(log2, log2)
        return abs(P_xy) / (np.sqrt(P_xx) * np.sqrt(P_yy) + 1e-8)

    def _spectral_subtract(self, mic_mag, ref_mag, scale, coh, client_listen_mode):
        """频域谱减（过减 + 半波整流）；realtime 更激进消回声、残差地板更低。"""
        raw_coef = 1.0 + scale * 3 + (coh - 0.97) * 30
        coef = max(0.5, min(4.0, raw_coef))
        echo_mag = ref_mag * scale * coef
        over_subtract = 2.5 if client_listen_mode == "realtime" else 2.0
        _resid_floor = 0.02 if client_listen_mode == "realtime" else 0.08
        return np.maximum(mic_mag - echo_mag * over_subtract, mic_mag * _resid_floor)

    def _update_aec_echo_baseline(self, mic_rms, residual_rms):
        """更新自适应回声基线（原始麦回声电平 + 消回声后残差电平）。"""
        if not hasattr(self, "_echo_baseline_rms") or self._echo_baseline_rms <= 0:
            self._echo_baseline_rms = float(mic_rms)
        else:
            self._echo_baseline_rms = self._echo_baseline_rms * 0.6 + float(mic_rms) * 0.4
        _HUMAN_CAP = 300.0
        _base = getattr(self, "_aec_residual_baseline_rms", 0.0)
        if _base <= 0:
            self._aec_residual_baseline_rms = max(float(residual_rms), 40.0)
        elif residual_rms >= _HUMAN_CAP:
            self._aec_residual_baseline_rms = _base * 0.995
        else:
            self._aec_residual_baseline_rms = _base * 0.9 + float(residual_rms) * 0.1

    def _apply_aec(self, timestamp: int, pcm_frame: bytes) -> bytes:
        """服务端 AEC（复果自研）：互相关延迟估计 + 频域谱减法消除 AI 回声参考。

        把设备上行麦帧与「TTS 下行参考」（服务端 AEC 参考缓存中的 PCM）做频域谱减，
        输出残差喂给 ASR / barge-in。数值管线与既有实现逐位一致；能量基准与诊断事件不变。
        """
        try:
            import numpy as np

            self._aec_last_effective = False

            if not pcm_frame or len(pcm_frame) == 0:
                self._aec_diag_event("empty_frame")
                return pcm_frame

            if not hasattr(self, "aec_audio_cache") or not self.aec_audio_cache:
                self._aec_diag_event("no_cache")
                return pcm_frame

            mic_audio = np.frombuffer(pcm_frame, dtype=np.int16).astype(np.float32)
            mic_rms = np.sqrt(np.mean(mic_audio ** 2))
            self._raw_mic_rms = float(mic_rms)
            self._aec_residual_rms = float(mic_rms)
            if getattr(self, "config", None) and self.config.get("aec_diag_enabled", False):
                _d = getattr(self, "_aec_diag", None)
                if _d is not None:
                    _d.setdefault("raw_mic", []).append(float(mic_rms))
                    if len(_d["raw_mic"]) > 200:
                        _d["raw_mic"] = _d["raw_mic"][-200:]

            if mic_rms < 30:
                self._aec_diag_event("mic_quiet", mic=float(mic_rms))
                return pcm_frame

            if not hasattr(self, "_aec_filter_state"):
                self._aec_filter_state = np.zeros(512)
                self._aec_delay_ms = 200
                self._aec_last_delay = 200
                self._aec_coherence_history = []

            sorted_timestamps = sorted(self.aec_audio_cache.keys())
            n = len(mic_audio)

            # 时延窗口匹配参考帧：参考晚于麦帧到达，允许 [-30ms, +AEC_MAX_DELAY_MS] 窗口。
            _cfg_aec = getattr(self, "config", None)
            AEC_MAX_DELAY_MS = int(_cfg_aec.get("aec_max_delay_ms", 2000)) if _cfg_aec else 2000
            if AEC_MAX_DELAY_MS < 2000:
                AEC_MAX_DELAY_MS = 2000
            candidates = [
                ts for ts in sorted_timestamps
                if -30 <= (timestamp - ts) <= AEC_MAX_DELAY_MS
            ]
            if not candidates:
                last_ref = sorted_timestamps[-1] if sorted_timestamps else None
                self._aec_diag_event(
                    "no_candidate",
                    dt=(timestamp - last_ref) if last_ref is not None else -99999,
                )
                return pcm_frame

            best_corr = -1
            best_ts = candidates[0]
            for cts in candidates:
                test_ref = np.frombuffer(self.aec_audio_cache[cts], dtype=np.int16).astype(np.float32)
                if np.sqrt(np.mean(test_ref ** 2)) < 50:
                    continue
                corr = self.log_power_spectrum_corr(mic_audio, test_ref)
                if corr > best_corr:
                    best_corr = corr
                    best_ts = cts

            best_ref = np.frombuffer(self.aec_audio_cache[best_ts], dtype=np.int16).astype(np.float32)
            ref_rms = np.sqrt(np.mean(best_ref ** 2))

            if ref_rms < 50:
                self._aec_diag_event("ref_quiet", ref=float(ref_rms), dt=(timestamp - best_ts))
                return pcm_frame

            aligned_ref = best_ref[:n]
            if len(aligned_ref) < n:
                aligned_ref = np.pad(aligned_ref, (0, n - len(aligned_ref)))

            window = np.hanning(n)
            mic_fft = np.fft.rfft(mic_audio * window)
            ref_fft = np.fft.rfft(aligned_ref * window)

            mic_mag = np.abs(mic_fft)
            ref_mag = np.abs(ref_fft)
            mic_phase = np.angle(mic_fft)

            scale = np.sum(mic_mag * ref_mag) / (np.dot(ref_mag, ref_mag) + 1e-8)
            coh = best_corr
            ref_power = ref_rms

            result_mag = self._spectral_subtract(mic_mag, ref_mag, scale, coh,
                                            getattr(self, "client_listen_mode", ""))

            self._aec_last_effective = True
            _baseline_streak = int(self.config.get("bargein_baseline_streak", 2))
            if getattr(self, "_aec_eff_streak", 0) >= _baseline_streak:
                self._baseline_ready = True

            result_fft = result_mag * np.exp(1j * mic_phase)
            output = np.fft.irfft(result_fft, n)

            if coh >= 0.97 and ref_power > 500:
                output = output * (0.15 if getattr(self, "client_listen_mode", "") == "realtime" else 0.3)

            output = np.clip(output, -32768, 32767)
            result = output.astype(np.int16).tobytes()

            residual_rms = float(np.sqrt(np.mean(output ** 2)))
            self._aec_residual_rms = residual_rms
            _ema = getattr(self, "_aec_residual_rms_ema", None)
            if _ema is None:
                self._aec_residual_rms_ema = float(residual_rms)
            else:
                self._aec_residual_rms_ema = _ema * 0.8 + float(residual_rms) * 0.2
            self._aec_residual_rms_smooth = self._aec_residual_rms_ema
            self._update_aec_echo_baseline(mic_rms, residual_rms)

            self._aec_diag_event(
                "effective",
                dt=(timestamp - best_ts),
                coh=float(best_corr),
                scale=float(scale),
                mic=float(mic_rms),
                ref=float(ref_rms),
                residual=residual_rms,
            )

            return result

        except Exception as e:
            self._aec_diag_event("exception")
            self.logger.bind(tag=TAG).warning(f"[AEC] 处理失败: {e}")
            return pcm_frame

    def _aec_diag_event(self, event, **extra):
        """AEC/barge-in 诊断（aec_diag_enabled 时约 1s 汇总一次，不改动任何音频处理逻辑）。"""
        if not getattr(self, "config", None) or not self.config.get("aec_diag_enabled", False):
            return
        d = getattr(self, "_aec_diag", None)
        if d is None:
            d = self._aec_diag = {
            "n": 0, "events": {}, "last_log": 0.0,
            "dt": [], "coh": [], "scale": [], "mic": [], "ref": [], "raw_mic": [], "residual": [],
        }
        d["n"] += 1
        d["events"][event] = d["events"].get(event, 0) + 1
        for k in ("dt", "coh", "scale", "mic", "ref", "residual"):
            if k in extra:
                d[k].append(extra[k])
                if len(d[k]) > 200:
                    d[k] = d[k][-200:]
        now = time.time()
        if now - d["last_log"] >= 1.0:
            d["last_log"] = now
            ev = d["events"]

            def _avg(x):
                return round(sum(x) / len(x), 2) if x else 0

            self.logger.bind(tag=TAG).info(
                f"[AEC-DIAG] frames={d['n']} 事件={ev} "
                f"Δt_avg={_avg(d['dt'])}ms coh_avg={_avg(d['coh'])} "
                f"scale_avg={_avg(d['scale'])} mic_rms_avg={_avg(d['mic'])} "
                f"ref_rms_avg={_avg(d['ref'])} residual_rms_avg={_avg(d.get('residual', []))} "
                f"raw_mic_rms_avg={_avg(d.get('raw_mic', []))}"
            )

    def _decode_opus_packet(self, opus_packet: bytes) -> bytes:
        """把固件上行的 opus 包解码为 960 样本/帧的 PCM。

        契约：空包直接返回 None；解码失败记 debug 并返回 None，不抛异常。每连接持有独立解码器。
        """
        try:
            if not opus_packet or len(opus_packet) == 0:
                return None
            self._init_connection_state(self)
            pcm_frame = self._connection_opus_decoder.decode(opus_packet, 960)
            return pcm_frame
        except Exception as e:
            self.logger.bind(tag=TAG).debug(f"Opus解码失败: {e}")
            return None

    def _init_connection_state(self, conn):
        """为连接初始化独立的 Opus 解码器"""
        if not hasattr(conn, "_connection_opus_decoder"):
            conn._connection_opus_decoder = opuslib_next.Decoder(16000, 1)

    # 重启频控：防止已认证设备反复触发 server/restart 形成 DoS
    _LAST_RESTART_TS = 0.0

    async def handle_restart(self, message):
        """处理服务器重启请求（加固：绝对入口路径 + 子进程存活确认 + 频控）"""
        global _LAST_RESTART_TS
        try:
            # 频控：10s 内重复触发直接忽略，避免 DoS
            if time.time() - _LAST_RESTART_TS < 10.0:
                self.logger.bind(tag=TAG).warning("重启指令过于频繁，已忽略（频控 10s）")
                await self.safe_send(
                    json.dumps(
                        {
                            "type": "server",
                            "status": "error",
                            "message": "Restart throttled",
                            "content": {"action": "restart"},
                        }
                    )
                )
                return

            self.logger.bind(tag=TAG).info("收到服务器重启指令，准备执行...")

            await self.safe_send(
                json.dumps(
                    {
                        "type": "server",
                        "status": "success",
                        "message": "服务器重启中...",
                        "content": {"action": "restart"},
                    }
                )
            )

            def restart_server():
                global _LAST_RESTART_TS
                # 用启动时的绝对入口路径，避免依赖 cwd 导致 FileNotFoundError
                app_path = os.path.abspath(sys.argv[0])
                time.sleep(1)
                self.logger.bind(tag=TAG).info("执行服务器重启...")
                try:
                    proc = subprocess.Popen(
                        [sys.executable, app_path],
                        stdin=sys.stdin,
                        stdout=sys.stdout,
                        stderr=sys.stderr,
                        start_new_session=True,
                    )
                except Exception as _spawn_err:
                    self.logger.bind(tag=TAG).error("重启子进程启动失败: {}", _spawn_err)
                    return
                # 确认子进程确实拉起再退出父进程，避免新旧进程皆无的全停窗口
                if proc.poll() is not None:
                    self.logger.bind(tag=TAG).error(
                        "重启子进程启动后立即退出（端口占用？），保留原进程继续服务"
                    )
                    return
                _LAST_RESTART_TS = time.time()
                os._exit(0)

            threading.Thread(target=restart_server, daemon=True).start()

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"重启失败: {str(e)}")
            await self.safe_send(
                json.dumps(
                    {
                        "type": "server",
                        "status": "error",
                        "message": f"Restart failed: {str(e)}",
                        "content": {"action": "restart"},
                    }
                )
            )

    def _initialize_components(self):
        try:
            if self.tts is None:
                self.tts = self._initialize_tts()
            asyncio.run_coroutine_threadsafe(
                self.tts.open_audio_channels(self), self.loop
            )
            if self.need_bind:
                self.bind_completed_event.set()
                return
            self.selected_module_str = build_module_string(
                self.config.get("selected_module", {})
            )
            self.logger = create_connection_logger(self.selected_module_str).bind(device_id=self.device_id)

            if self.config.get("prompt") is not None:
                user_prompt = self.config["prompt"]
                prompt = self.prompt_manager.get_quick_prompt(user_prompt)
                self.change_system_prompt(prompt)
                self.logger.bind(tag=TAG).info(
                    f"快速初始化组件: prompt成功 {prompt[:50]}..."
                )

            if self.vad is None:
                self.vad = self._vad
            if self.asr is None:
                self.asr = self._initialize_asr()

            self._initialize_voiceprint()
            asyncio.run_coroutine_threadsafe(
                self.asr.open_audio_channels(self), self.loop
            )

            self._initialize_memory()
            self._initialize_intent()
            self._init_report_threads()
            self._init_prompt_enhancement()
            self._inject_tool_call_fewshot()

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"实例化组件失败: {e}")
            raise

    def _init_prompt_enhancement(self):
        self.prompt_manager.update_context_info(self, self.client_ip)
        enhanced_prompt = self.prompt_manager.build_enhanced_prompt(
            self.config["prompt"],
            self.device_id,
            self.client_ip,
            emoji_enabled=(self.features or {}).get("emoji", True),
        )
        if enhanced_prompt:
            self.change_system_prompt(enhanced_prompt)
            self.logger.bind(tag=TAG).debug("系统提示词已增强更新")

    def _inject_tool_call_fewshot(self):
        """注入工具调用 few-shot 示例到对话历史。

        正样本（工具调用示例）放在动态 system 之前，可命中前缀缓存；
        负样本（直接回答示例）放在动态 system 之后、紧挨真实用户消息。
        """
        if self.intent_type != "function_call":
            return
        if not hasattr(self, "func_handler") or self.func_handler is None:
            return

        tools = self.func_handler.get_functions()
        if not tools:
            return

        tool_names = {t.get("function", {}).get("name") for t in tools}

        # 示例1：direct_answer（回复内容写在 response 参数里，无需递归）
        da_tc_id = "fewshot_da_001"
        self.dialogue.put(Message(role="user", content="给我讲个故事吧", is_temporary=True))
        self.dialogue.put(Message(
            role="assistant",
            tool_calls=[{
                "id": da_tc_id,
                "function": {"arguments": '{"response": "好呀，你想听什么类型的呀？童话、冒险还是搞笑的？选一个我给你开讲~"}', "name": "direct_answer"},
                "type": "function", "index": 0,
            }],
            is_temporary=True,
        ))
        self.dialogue.put(Message(
            role="tool", tool_call_id=da_tc_id,
            content="已直接回复", is_temporary=True,
        ))

        # 示例2：真实工具调用（handle_exit_intent）
        if "handle_exit_intent" in tool_names:
            tc_id = "fewshot_exit_001"
            self.dialogue.put(Message(role="user", content="拜拜", is_temporary=True))
            self.dialogue.put(Message(
                role="assistant",
                tool_calls=[{
                    "id": tc_id,
                    "function": {"arguments": '{"say_goodbye": "再见，下次再聊~"}', "name": "handle_exit_intent"},
                    "type": "function", "index": 0,
                }],
                is_temporary=True,
            ))
            self.dialogue.put(Message(
                role="tool", tool_call_id=tc_id,
                content="退出意图已处理", is_temporary=True,
            ))
            self.dialogue.put(Message(
                role="assistant", content="再见，下次再聊~", is_temporary=True,
            ))

        self.logger.bind(tag=TAG).debug("已注入工具调用 few-shot 示例")

    def _init_report_threads(self):
        """初始化 ASR 和 TTS 上报线程"""
        if not self.read_config_from_api or self.need_bind:
            return
        if self.chat_history_conf == 0:
            return
        if self.report_thread is None or not self.report_thread.is_alive():
            self.report_thread = threading.Thread(
                target=self._report_worker, daemon=True
            )
            self.report_thread.start()
            self.logger.bind(tag=TAG).info("TTS上报线程已启动")

    def _initialize_tts(self):
        """初始化 TTS"""
        tts = None
        if not self.need_bind:
            tts = initialize_tts(self.config)
        if tts is None:
            tts = DefaultTTS(self.config, delete_audio_file=True)
        return tts

    def _initialize_asr(self):
        """初始化 ASR"""
        if (
                self._asr is not None
                and hasattr(self._asr, "interface_type")
                and self._asr.interface_type == InterfaceType.LOCAL
        ):
            # 本地一个实例 ASR 可被多个连接共享
            asr = self._asr
        else:
            # 远程 ASR 涉及 websocket 连接和接收线程，需要每个连接一个实例
            asr = initialize_asr(self.config)
        return asr

    def _initialize_voiceprint(self):
        """为当前连接初始化声纹识别（复用全局单例 provider）"""
        try:
            provider = get_voiceprint_provider(self.config)
            if provider is not None and provider.enabled:
                self.voiceprint_provider = provider
                self.logger.bind(tag=TAG).info("声纹识别功能已在连接时动态启用")
            else:
                self.logger.bind(tag=TAG).info("声纹识别功能未启用")
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"声纹识别初始化失败: {str(e)}")

    def _voiceprint_prompt_config(self) -> dict:
        """为 system prompt 构造说话人信息：把注册表昵称拼成旧式 'id,name,description'
        字符串，复用 dialogue._render_dynamic_part 既有解析。未启用/无登记时退化为原始配置块。"""
        base = self.config.get("voiceprint", {}) or {}
        vp = self.voiceprint_provider
        if vp is None or not getattr(vp, "enabled", False):
            return base
        try:
            speakers = [f"{s['voiceprint_id']},{s['name']}," for s in vp.speakers]
            if speakers:
                enriched = dict(base)
                enriched["speakers"] = speakers
                return enriched
        except Exception:
            pass
        return base

    def _watch_task(self, task, name=""):
        """统一兜底 fire-and-forget 任务：未处理异常落日志 + metrics，避免静默失败。"""
        def _on_done(t):
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"后台任务异常[{name}]: {e}")
                try:
                    METRICS.inc("fuguoai_bg_task_errors_total", 1.0,
                                labels={"task": name or "unknown"})
                except Exception:
                    pass
        task.add_done_callback(_on_done)
        return task

    def _on_components_init_done(self, fut):
        """组件后台初始化完成的回调：异常须可见（旧实现吞掉且 future 无人查）。"""
        try:
            fut.result()
        except Exception as e:
            self.logger.bind(tag=TAG).error(
                f"组件初始化失败(连接将以最小功能运行, 设备将重连): {e}")
            try:
                METRICS.inc("fuguoai_component_init_errors_total", 1.0,
                            labels={"device_id": self.device_id or "unknown"})
            except Exception:
                pass

    async def _background_initialize(self):
        """在后台初始化配置和组件（完全不阻塞主循环）"""
        try:
            await self._initialize_private_config_async()
            fut = self.executor.submit(self._initialize_components)
            fut.add_done_callback(self._on_components_init_done)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"后台初始化失败: {e}")

    async def _initialize_private_config_async(self):
        """从接口异步获取差异化配置（异步版本，不阻塞主循环）"""
        if not self.read_config_from_api:
            self.need_bind = False
            self.bind_completed_event.set()
            return
        try:
            begin_time = time.time()
            private_config = await get_private_config_from_api(
                self.config,
                self.headers.get("device-id"),
                self.headers.get("client-id", self.headers.get("device-id")),
            )
            private_config["delete_audio"] = bool(self.config.get("delete_audio", True))
            self.logger.bind(tag=TAG).info(
                f"{time.time() - begin_time} 秒，异步获取差异化配置成功: {json.dumps(filter_sensitive_info(private_config), ensure_ascii=False)}"
            )
            self.need_bind = False
            self.bind_completed_event.set()
        except DeviceNotFoundException as e:
            self.need_bind = True
            private_config = {}
        except DeviceBindException as e:
            self.need_bind = True
            self.bind_code = e.bind_code
            private_config = {}
        except Exception as e:
            self.need_bind = True
            self.logger.bind(tag=TAG).error(f"异步获取差异化配置失败: {e}")
            private_config = {}

        init_llm, init_tts, init_memory, init_intent = (
            False,
            False,
            False,
            False,
        )

        init_vad = check_vad_update(self.common_config, private_config)
        init_asr = check_asr_update(self.common_config, private_config)

        if init_vad:
            self.config["VAD"] = private_config["VAD"]
            self.config["selected_module"]["VAD"] = private_config["selected_module"]["VAD"]
        if init_asr:
            self.config["ASR"] = private_config["ASR"]
            self.config["selected_module"]["ASR"] = private_config["selected_module"]["ASR"]
        if private_config.get("TTS", None) is not None:
            init_tts = True
            self.config["TTS"] = private_config["TTS"]
            self.config["selected_module"]["TTS"] = private_config["selected_module"]["TTS"]
        if private_config.get("LLM", None) is not None:
            init_llm = True
            self.config["LLM"] = private_config["LLM"]
            self.config["selected_module"]["LLM"] = private_config["selected_module"]["LLM"]
        if private_config.get("VLLM", None) is not None:
            self.config["VLLM"] = private_config["VLLM"]
            self.config["selected_module"]["VLLM"] = private_config["selected_module"]["VLLM"]
        if private_config.get("Memory", None) is not None:
            init_memory = True
            self.config["Memory"] = private_config["Memory"]
            self.config["selected_module"]["Memory"] = private_config["selected_module"]["Memory"]
        if private_config.get("Intent", None) is not None:
            init_intent = True
            self.config["Intent"] = private_config["Intent"]
            model_intent = private_config.get("selected_module", {}).get("Intent", {})
            self.config["selected_module"]["Intent"] = model_intent
            if model_intent != "Intent_nointent":
                plugin_from_server = private_config.get("plugins", {})
                for plugin, config_str in plugin_from_server.items():
                    plugin_from_server[plugin] = json.loads(config_str)
                self.config["plugins"] = plugin_from_server
                self.config["Intent"][self.config["selected_module"]["Intent"]]["functions"] = plugin_from_server.keys()
        if private_config.get("prompt", None) is not None:
            self.config["prompt"] = private_config["prompt"]
        if private_config.get("voiceprint", None) is not None:
            self.config["voiceprint"] = private_config["voiceprint"]
        if private_config.get("summaryMemory", None) is not None:
            self.config["summaryMemory"] = private_config["summaryMemory"]
        if private_config.get("device_max_output_size", None) is not None:
            self.max_output_size = int(private_config["device_max_output_size"])
        if private_config.get("chat_history_conf", None) is not None:
            self.chat_history_conf = int(private_config["chat_history_conf"])
        if private_config.get("mcp_endpoint", None) is not None:
            self.config["mcp_endpoint"] = private_config["mcp_endpoint"]
        if private_config.get("context_providers", None) is not None:
            self.config["context_providers"] = private_config["context_providers"]

        # 注入替换词到 TTS 模块配置
        if private_config.get("correct_words", None) is not None:
            select_tts_module = self.config["selected_module"]["TTS"]
            self.config["TTS"][select_tts_module]["correct_words"] = private_config["correct_words"]

        try:
            modules = await self.loop.run_in_executor(
                None,
                initialize_modules,
                self.logger,
                private_config,
                init_vad,
                init_asr,
                init_llm,
                init_tts,
                init_memory,
                init_intent,
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"初始化组件失败: {e}")
            modules = {}
        if modules.get("tts", None) is not None:
            self.tts = modules["tts"]
        if modules.get("vad", None) is not None:
            self.vad = modules["vad"]
        if modules.get("asr", None) is not None:
            self.asr = modules["asr"]
        if modules.get("llm", None) is not None:
            llm_inst = modules["llm"]
            # A/B 分桶包装（按 device_id 稳定分桶，B 桶注入采样覆盖）
            if self.device_id:
                try:
                    ab = get_ab_test(self.config)
                    if ab.enabled:
                        llm_inst = ab.wrap(self.device_id, llm_inst)
                        self._ab_bucket = ab.bucket_for(self.device_id)
                except Exception:
                    pass
            self.llm = llm_inst
        # 对话自评估单例（LLM-as-judge 后台打分，默认 disabled）。
        # 强制 reset 后用完整 self.config 重建：避免被无参调用在 config 未就绪时抢先锁死单例。
        try:
            import fuguoai_core.utils.dialogue_eval as _dem
            _dem._eval_instance = None
            self.dialogue_eval = get_dialogue_eval(self.config)
        except Exception:
            self.dialogue_eval = None
        # 设备人格单例（按 device_id 三层合并画像，默认 disabled）
        try:
            import fuguoai_core.utils.persona as _pm
            _pm._instance = None
            self.persona_store = get_persona_store(self.config)
        except Exception:
            self.persona_store = None
        # 家庭 RAG 知识库单例（对话抽取 + 向量检索，默认 disabled）
        try:
            self.family_rag = get_family_rag(self.config)
        except Exception:
            self.family_rag = None
        # 复果品牌知识库单例（brand_kb：core 每轮注入 + search_brand_kb 工具按需查，默认 disabled）
        try:
            from fuguoai_core.utils.brand_kb import get_brand_kb
            self.brand_kb = get_brand_kb(self.config)
        except Exception:
            self.brand_kb = None
        if not hasattr(self, "_ab_bucket"):
            self._ab_bucket = "A"
        if modules.get("intent", None) is not None:
            self.intent = modules["intent"]
        if modules.get("memory", None) is not None:
            self.memory = modules["memory"]

    def _initialize_memory(self):
        if self.memory is None:
            return
        self.memory.init_memory(
            role_id=self.device_id,
            llm=self.llm,
            summary_memory=self.config.get("summaryMemory", None),
            save_to_file=not self.read_config_from_api,
        )

        memory_config = self.config["Memory"]
        memory_type = self.config["Memory"][self.config["selected_module"]["Memory"]]["type"]
        if memory_type == "nomem" or memory_type == "mem_report_only":
            return
        elif memory_type == "mem_local_short":
            memory_llm_name = memory_config[self.config["selected_module"]["Memory"]]["llm"]
            if memory_llm_name and memory_llm_name in self.config["LLM"]:
                from fuguoai_core.utils import llm as llm_utils
                memory_llm_config = self.config["LLM"][memory_llm_name]
                memory_llm_type = memory_llm_config.get("type", memory_llm_name)
                memory_llm = llm_utils.create_instance(
                    memory_llm_type, memory_llm_config
                )
                self.logger.bind(tag=TAG).info(
                    f"为记忆总结创建了专用LLM: {memory_llm_name}, 类型: {memory_llm_type}"
                )
                self.memory.set_llm(memory_llm)
            else:
                self.memory.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("使用主LLM作为意图识别模型")

    def _initialize_intent(self):
        if self.intent is None:
            return
        self.intent_type = self.config["Intent"][
            self.config["selected_module"]["Intent"]
        ]["type"]
        if self.intent_type == "function_call" or self.intent_type == "intent_llm":
            self.load_function_plugin = True
        intent_config = self.config["Intent"]
        intent_type = self.config["Intent"][self.config["selected_module"]["Intent"]]["type"]

        if intent_type == "nointent":
            return
        elif intent_type == "intent_llm":
            intent_llm_name = intent_config[self.config["selected_module"]["Intent"]]["llm"]

            if intent_llm_name and intent_llm_name in self.config["LLM"]:
                from fuguoai_core.utils import llm as llm_utils
                intent_llm_config = self.config["LLM"][intent_llm_name]
                intent_llm_type = intent_llm_config.get("type", intent_llm_name)
                intent_llm = llm_utils.create_instance(
                    intent_llm_type, intent_llm_config
                )
                self.logger.bind(tag=TAG).info(
                    f"为意图识别创建了专用LLM: {intent_llm_name}, 类型: {intent_llm_type}"
                )
                self.intent.set_llm(intent_llm)
            else:
                self.intent.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("使用主LLM作为意图识别模型")

        self.func_handler = UnifiedToolHandler(self)
        if hasattr(self, "loop") and self.loop:
            asyncio.run_coroutine_threadsafe(self.func_handler._initialize(), self.loop)

    def change_system_prompt(self, prompt):
        self.prompt = prompt
        self.dialogue.update_system_message(self.prompt)

    def _filter_tools_by_context(self, functions, context_text):
        """按需裁剪低频查询类工具 schema。

        门控由 config[ToolGate].enabled 控制（默认开）。被门控工具仅当 context 命中触发词、
        或近期对话已调用过时才下发；其余工具常发。
        """
        gate_cfg = (self.config.get("ToolGate", {}) or {})
        if not gate_cfg.get("enabled", True):
            return functions
        ctx = (context_text or "").lower()
        used = set()
        try:
            for m in self.dialogue.dialogue:
                tcs = getattr(m, "tool_calls", None)
                if tcs:
                    for tc in tcs:
                        fn = getattr(tc, "function", None)
                        if fn and getattr(fn, "name", None):
                            used.add(fn.name)
        except Exception:
            pass
        out = []
        for f in functions:
            name = f.get("function", {}).get("name", "")
            kws = _TOOL_GATE.get(name)
            if kws is None:
                for _pfx, _pkws in _TOOL_GATE_PREFIX.items():
                    if name.startswith(_pfx):
                        kws = _pkws
                        break
            if kws is None or name in used or any(k in ctx for k in kws):
                out.append(f)
        dropped = len(functions) - len(out)
        if dropped:
            self.logger.bind(tag=TAG).debug(f"工具门控裁剪 {dropped} 个(剩余 {len(out)}): {[f.get('function',{}).get('name') for f in functions if f not in out]}")
        return out

    def chat(self, query, depth=0):
        # 保存当前任务的 sentence_id 到局部变量，避免被新任务覆盖
        current_sentence_id = None

        if query is not None:
            self.logger.bind(tag=TAG).info(f"大模型收到用户消息: {query}")

        # 为最顶层时新建会话 ID 和发送 FIRST 请求
        if depth == 0:
            current_sentence_id = str(uuid.uuid4().hex)
            self.sentence_id = current_sentence_id
            self.dialogue.put(Message(role="user", content=query))
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.FIRST,
                    content_type=ContentType.ACTION,
                )
            )
            # 打首包延迟计时起点
            self._first_audio_mark = time.monotonic()
            self._first_audio_recorded = False
            self._llm_first_token_recorded = False
        else:
            current_sentence_id = self.sentence_id

        # 设置最大递归深度，避免无限循环
        MAX_DEPTH = 5
        force_final_answer = False

        if depth >= MAX_DEPTH:
            self.logger.bind(tag=TAG).debug(
                f"已达到最大工具调用深度 {MAX_DEPTH}，将强制基于现有信息回答"
            )
            force_final_answer = True
            self.dialogue.put(
                Message(
                    role="user",
                    content="[系统提示] 已达到最大工具调用次数限制，请你基于目前已经获取的所有信息，直接给出最终答案。不要再尝试调用任何工具。",
                )
            )

        functions = None
        if (
                self.intent_type == "function_call"
                and hasattr(self, "func_handler")
                and not force_final_answer
        ):
            functions = list(self.func_handler.get_functions())
            _orig_n = len(functions)
            try:
                _ctx = (query or "")
                for _m in list(self.dialogue.dialogue)[-8:]:
                    if getattr(_m, "role", None) == "user" and getattr(_m, "content", None):
                        _ctx += " " + _m.content
                functions = self._filter_tools_by_context(functions, _ctx)
                self.logger.bind(tag=TAG).info(
                    f"工具门控：原始 {_orig_n} 个 → 下发 {len(functions)} 个（裁剪 {_orig_n - len(functions)} 个）"
                )
            except Exception:
                pass
            # 仅在第一层调用时注入 direct_answer 虚拟工具
            if functions is not None and depth == 0:
                functions.append(DIRECT_ANSWER_TOOL)

        response_message = []

        try:
            memory_str = None
            # 仅当 query 非空（代表用户询问）时查询记忆
            if self.memory is not None and query:
                future = asyncio.run_coroutine_threadsafe(
                    self.memory.query_memory(query, role_id=self.device_id), self.loop
                )
                try:
                    # 记忆查询超时压缩：记忆为对话增强、非必需，超时即降级为空记忆继续
                    memory_str = future.result(timeout=3)
                except Exception:
                    memory_str = None

            # 设备人格注入——与记忆同通道进动态 system（<persona> 块），异常降级为不注入
            try:
                _pstore = getattr(self, "persona_store", None)
                if _pstore is not None:
                    persona_block = _pstore.render(self.device_id)
                    if persona_block:
                        memory_str = (
                            persona_block + "\n" + memory_str if memory_str else persona_block
                        )
            except Exception:
                pass

            # 家庭知识库注入——与记忆/人格同通道进动态 system（<family_knowledge> 块）
            try:
                _frag = getattr(self, "family_rag", None)
                if _frag is not None and query:
                    family_block = _frag.render(self.device_id, query)
                    if family_block:
                        memory_str = (
                            family_block + "\n" + memory_str if memory_str else family_block
                        )
            except Exception:
                pass

            # 复果品牌核心知识注入——core 条目每轮保底常驻（<brand_knowledge> 块）
            try:
                _bkb = getattr(self, "brand_kb", None)
                if _bkb is not None:
                    brand_block = _bkb.render_core()
                    if brand_block:
                        memory_str = (
                            brand_block + "\n" + memory_str if memory_str else brand_block
                        )
            except Exception:
                pass

            window_conf = (self.config.get("MemoryRAG", {}) or {}).get("context_window", None)

            if self.intent_type == "function_call" and functions is not None:
                llm_responses = self.llm.response_with_functions(
                    self.session_id,
                    self.dialogue.get_llm_dialogue_with_memory(
                        memory_str, self._voiceprint_prompt_config(), window_conf
                    ),
                    functions=functions,
                    device_id=self.device_id,
                    device_session_id=self.session_id,
                )
            else:
                llm_responses = self.llm.response(
                    self.session_id,
                    self.dialogue.get_llm_dialogue_with_memory(
                        memory_str, self._voiceprint_prompt_config(), window_conf
                    ),
                    device_id=self.device_id,
                    device_session_id=self.session_id,
                )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM 处理出错 {query}: {e}")
            # LLM 主+备双通道全失败时，下发离线兜底话术 + LAST 收尾包
            self._send_offline_fallback(depth)
            return None

        # 处理流式响应
        tool_call_flag = False
        tool_calls_list = []
        content_arguments = ""
        emotion_flag = True
        try:
            for response in llm_responses:
                if self.client_abort:
                    break
                if self.intent_type == "function_call" and functions is not None:
                    content, tools_call = response
                    if "content" in response:
                        content = response["content"]
                        tools_call = None
                    if content is not None and len(content) > 0:
                        content_arguments += content
                        if not getattr(self, "_llm_first_token_recorded", False):
                            _ftm = getattr(self, "_first_audio_mark", None)
                            if _ftm is not None:
                                self._llm_first_token_recorded = True
                                self.logger.bind(tag=TAG).info(f"LLM首token延迟: {time.monotonic() - _ftm:.3f}s")

                    if not tool_call_flag and content_arguments.startswith("<tool_call>"):
                        tool_call_flag = True

                    if tools_call is not None and len(tools_call) > 0:
                        tool_call_flag = True
                        self._merge_tool_calls(tool_calls_list, tools_call)

                    # 流式提取 direct_answer 的 response 参数，实时送 TTS
                    _DA_STREAM_BUFFER = 5
                    for tc in tool_calls_list:
                        if tc["name"] == "direct_answer" and tc.get("arguments"):
                            da_text = self._extract_direct_answer_response(tc["arguments"])
                            sent_len = tc.get("_da_sent", 0)
                            if da_text and len(da_text) > sent_len:
                                safe_end = max(sent_len, len(da_text) - _DA_STREAM_BUFFER)
                                if safe_end > sent_len:
                                    new_part = da_text[sent_len:safe_end]
                                    new_part = self._clean_response_garbage(new_part)
                                    if new_part:
                                        tc["_da_sent"] = safe_end
                                        self.tts.tts_text_queue.put(
                                            TTSMessageDTO(
                                                sentence_id=current_sentence_id,
                                                sentence_type=SentenceType.MIDDLE,
                                                content_type=ContentType.TEXT,
                                                content_detail=new_part,
                                            )
                                        )
                else:
                    content = response

                # 在 llm 回复中获取情绪表情，一轮对话只在开头获取一次
                if emotion_flag and content is not None and content.strip():
                    if (self.features or {}).get("emoji", True):
                        asyncio.run_coroutine_threadsafe(
                            textUtils.get_emotion(self, content),
                            self.loop,
                        )
                    emotion_flag = False

                if content is not None and len(content) > 0:
                    if not getattr(self, "_llm_first_token_recorded", False):
                        _ftm = getattr(self, "_first_audio_mark", None)
                        if _ftm is not None:
                            self._llm_first_token_recorded = True
                            self.logger.bind(tag=TAG).info(f"LLM首token延迟: {time.monotonic() - _ftm:.3f}s")
                    if not tool_call_flag:
                        response_message.append(content)
                        self.tts.tts_text_queue.put(
                            TTSMessageDTO(
                                sentence_id=current_sentence_id,
                                sentence_type=SentenceType.MIDDLE,
                                content_type=ContentType.TEXT,
                                content_detail=content,
                            )
                        )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM stream processing error: {e}")
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.MIDDLE,
                    content_type=ContentType.TEXT,
                    content_detail=get_system_error_response(self.config),
                )
            )
            if depth == 0:
                self.tts.tts_text_queue.put(
                    TTSMessageDTO(
                        sentence_id=current_sentence_id,
                        sentence_type=SentenceType.LAST,
                        content_type=ContentType.ACTION,
                    )
                )
            return
        # 处理 function call
        if tool_call_flag:
            bHasError = False
            # 处理基于文本的工具调用格式
            if len(tool_calls_list) == 0 and content_arguments:
                a = extract_json_from_string(content_arguments)
                if a is not None:
                    try:
                        content_arguments_json = json.loads(a)
                        tool_calls_list.append(
                            {
                                "id": str(uuid.uuid4().hex),
                                "name": content_arguments_json["name"],
                                "arguments": json.dumps(
                                    content_arguments_json["arguments"],
                                    ensure_ascii=False,
                                ),
                            }
                        )
                    except Exception as e:
                        bHasError = True
                        response_message.append(a)
                else:
                    bHasError = True
                    response_message.append(content_arguments)
                if bHasError:
                    self.logger.bind(tag=TAG).error(
                        f"function call error: {content_arguments}"
                    )

            if not bHasError and len(tool_calls_list) > 0:
                # 处理 direct_answer 虚拟工具
                direct_answer_calls = [tc for tc in tool_calls_list if tc["name"] == "direct_answer"]
                real_tool_calls = [tc for tc in tool_calls_list if tc["name"] != "direct_answer"]

                if direct_answer_calls:
                    self.logger.bind(tag=TAG).debug(
                        f"模型选择 direct_answer，流式已播报，写入对话历史"
                    )
                    for tc in direct_answer_calls:
                        da_response = self._extract_direct_answer_response(tc.get("arguments", "{}"))
                        if da_response:
                            sent_len = tc.get("_da_sent", 0)
                            remaining = da_response[sent_len:]
                            if remaining:
                                remaining = self._clean_response_garbage(remaining)
                                if remaining:
                                    self.tts.tts_text_queue.put(
                                        TTSMessageDTO(
                                            sentence_id=current_sentence_id,
                                            sentence_type=SentenceType.MIDDLE,
                                            content_type=ContentType.TEXT,
                                            content_detail=remaining,
                                        )
                                    )
                            da_response = self._clean_response_garbage(da_response)
                            self.tts.store_tts_text(current_sentence_id, da_response)
                            self.dialogue.put(Message(role="assistant", content=da_response))

                    if not real_tool_calls:
                        if depth == 0:
                            self.tts.tts_text_queue.put(
                                TTSMessageDTO(
                                    sentence_id=current_sentence_id,
                                    sentence_type=SentenceType.LAST,
                                    content_type=ContentType.ACTION,
                                )
                            )
                        return

                    tool_calls_list = real_tool_calls

            if not bHasError and len(tool_calls_list) > 0:
                self.logger.bind(tag=TAG).debug(
                    f"检测到 {len(tool_calls_list)} 个工具调用"
                )

                # LLM 流式阶段已播报过的文本
                streamed_text = ""
                if len(response_message) > 0:
                    streamed_text = "".join(response_message)
                    self.tts.store_tts_text(current_sentence_id, streamed_text)
                    self.dialogue.put(Message(role="assistant", content=streamed_text))
                response_message.clear()

                futures_with_data = []
                for tool_call_data in tool_calls_list:
                    self.logger.bind(tag=TAG).debug(
                        f"function_name={tool_call_data['name']}, function_id={tool_call_data['id']}, function_arguments={tool_call_data['arguments']}"
                    )

                    try:
                        tool_input = json.loads(tool_call_data.get("arguments") or "{}")
                    except Exception as e:
                        self.logger.bind(tag=TAG).error(
                            f"工具参数 JSON 解析失败，跳过该工具调用: {e} | raw={tool_call_data.get('arguments')}"
                        )
                        continue
                    enqueue_tool_report(self, tool_call_data['name'], tool_input)

                    future = asyncio.run_coroutine_threadsafe(
                        self.func_handler.handle_llm_function_call(
                            self, tool_call_data
                        ),
                        self.loop,
                    )
                    futures_with_data.append((future, tool_call_data, tool_input))

                # 工具调用超时时间，可配置，默认 30 秒
                tool_call_timeout = int(self.config.get("tool_call_timeout", 30))
                tool_results = []

                for future, tool_call_data, tool_input in futures_with_data:
                    try:
                        result = future.result(timeout=tool_call_timeout)
                        tool_results.append((result, tool_call_data))
                        enqueue_tool_report(self, tool_call_data['name'], tool_input, str(result.result) if result.result else None, report_tool_call=False)
                    except Exception as e:
                        self.logger.bind(tag=TAG).error(
                            f"工具调用超时或异常: {tool_call_data['name']}, 错误: {e}"
                        )
                        tool_results.append((
                            ActionResponse(action=Action.ERROR, result="哎呀，网络遇到点问题，请稍后再试下！"),
                            tool_call_data
                        ))
                        enqueue_tool_report(self, tool_call_data['name'], tool_input, str(e), report_tool_call=False)

                if tool_results:
                    self._handle_function_result(tool_results, depth=depth, streamed_text=streamed_text)

        # 存储对话内容
        text_buff = "".join(response_message)
        if response_message:
            self.tts.store_tts_text(current_sentence_id, text_buff)
            self.dialogue.put(Message(role="assistant", content=text_buff))

        # 对话自评估（LLM-as-judge）后台打分，绝不阻塞主对话
        if (
            depth == 0
            and text_buff
            and getattr(self, "dialogue_eval", None) is not None
            and self.dialogue_eval.enabled
        ):
            try:
                bucket = getattr(self, "_ab_bucket", "A") or "A"
                asyncio.run_coroutine_threadsafe(
                    self.dialogue_eval.evaluate_async(
                        self.device_id, bucket, query, text_buff, self.config
                    ),
                    self.loop,
                )
            except Exception:
                pass

        # 家庭事实抽取（三段式后台 LLM）后台入库，绝不阻塞主对话
        if (
            depth == 0
            and text_buff
            and getattr(self, "family_rag", None) is not None
            and self.family_rag.enabled
            and self.family_rag.extract_enabled
        ):
            try:
                asyncio.run_coroutine_threadsafe(
                    self.family_rag.extract_async(
                        self.device_id, query, text_buff, self.config
                    ),
                    self.loop,
                )
            except Exception:
                pass

        if depth == 0:
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=current_sentence_id,
                    sentence_type=SentenceType.LAST,
                    content_type=ContentType.ACTION,
                )
            )
            self.logger.bind(tag=TAG).debug(
                lambda: json.dumps(
                    self.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False
                )
            )

        return True

    def _handle_function_result(self, tool_results, depth, streamed_text=""):
        need_llm_tools = []
        record_tools = []

        for result, tool_call_data in tool_results:
            if result.action in [
                Action.RESPONSE,
                Action.NOTFOUND,
                Action.ERROR,
            ]:
                text = result.response if result.response else result.result
                if streamed_text and text in streamed_text:
                    self.logger.bind(tag=TAG).debug(
                        f"Skipping duplicate TTS for tool {tool_call_data['name']}, already streamed"
                    )
                else:
                    self.tts.tts_one_sentence(self, ContentType.TEXT, content_detail=text)
                    self.tts.store_tts_text(self.sentence_id, text)
                self.dialogue.put(Message(role="assistant", content=text))
            elif result.action == Action.REQLLM:
                need_llm_tools.append((result, tool_call_data))
            elif result.action == Action.RECORD:
                record_tools.append((result, tool_call_data))
            else:
                pass

        # Action.RECORD：写入完整工具调用链（assistant(tool_calls) → tool(result) → assistant(response)）
        if record_tools:
            all_tool_calls = [
                {
                    "id": tool_call_data["id"],
                    "function": {
                        "arguments": (
                            "{}"
                            if tool_call_data["arguments"] == ""
                            else tool_call_data["arguments"]
                        ),
                        "name": tool_call_data["name"],
                    },
                    "type": "function",
                    "index": idx,
                }
                for idx, (_, tool_call_data) in enumerate(record_tools)
            ]
            self.dialogue.put(Message(role="assistant", tool_calls=all_tool_calls))

            for result, tool_call_data in record_tools:
                text = result.result or ""
                self.dialogue.put(
                    Message(
                        role="tool",
                        tool_call_id=(
                            str(uuid.uuid4())
                            if tool_call_data["id"] is None
                            else tool_call_data["id"]
                        ),
                        content=text,
                    )
                )

            response_parts = []
            for result, _ in record_tools:
                resp = result.response or result.result
                if resp:
                    response_parts.append(resp)
            if response_parts:
                self.dialogue.put(Message(role="assistant", content="，".join(response_parts)))

        if need_llm_tools:
            all_tool_calls = [
                {
                    "id": tool_call_data["id"],
                    "function": {
                        "arguments": (
                            "{}"
                            if tool_call_data["arguments"] == ""
                            else tool_call_data["arguments"]
                        ),
                        "name": tool_call_data["name"],
                    },
                    "type": "function",
                    "index": idx,
                }
                for idx, (_, tool_call_data) in enumerate(need_llm_tools)
            ]
            self.dialogue.put(Message(role="assistant", tool_calls=all_tool_calls))

            for result, tool_call_data in need_llm_tools:
                text = result.result
                if text is not None and len(text) > 0:
                    self.dialogue.put(
                        Message(
                            role="tool",
                            tool_call_id=(
                                str(uuid.uuid4())
                                if tool_call_data["id"] is None
                                else tool_call_data["id"]
                            ),
                            content=text,
                        )
                    )

            self.chat(None, depth=depth + 1)

    def _report_worker(self):
        """聊天记录上报工作线程"""
        while not self.stop_event.is_set():
            try:
                item = self.report_queue.get(timeout=1)
                if item is None:
                    break
                try:
                    if self.executor is None:
                        continue
                    self.executor.submit(self._process_report, *item)
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"聊天记录上报线程异常: {e}")
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"聊天记录上报工作线程异常: {e}")

        self.logger.bind(tag=TAG).info("聊天记录上报线程已退出")

    def _send_offline_fallback(self, depth=0):
        """LLM 主+备双通道全失败时的离线兜底：下发可配置话术 + LAST 收尾包，避免用户面对死寂。"""
        try:
            sid = getattr(self, "current_sentence_id", None)
            if sid is None:
                return
            text = build_offline_fallback(self.config)
            self.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=sid,
                    sentence_type=SentenceType.MIDDLE,
                    content_type=ContentType.TEXT,
                    content_detail=text,
                )
            )
            if depth == 0:
                self.tts.tts_text_queue.put(
                    TTSMessageDTO(
                        sentence_id=sid,
                        sentence_type=SentenceType.LAST,
                        content_type=ContentType.ACTION,
                    )
                )
            METRICS.inc(
                "fuguoai_offline_fallback_total",
                labels={"device_id": self.device_id or ""},
                help_text="LLM 双通道全失败时离线兜底触发计数",
            )
            self.logger.bind(tag=TAG).info(f"已下发离线兜底话术 device={self.device_id}")
        except Exception as ex:
            self.logger.bind(tag=TAG).error(f"离线兜底发送异常: {ex}")

    def _process_report(self, type, text, audio_data, report_time):
        """处理上报任务"""
        try:
            asyncio.run(report(self, type, text, audio_data, report_time))
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"上报处理异常: {e}")
        finally:
            self.report_queue.task_done()

    def clearSpeakStatus(self):
        """清除服务端 speaking 态（设备端据此从「说话中」回到「聆听中」）。"""
        self.client_is_speaking = False
        self.logger.bind(tag=TAG).debug(f"清除服务端讲话状态")

    async def safe_send(self, data):
        """线程/协程安全的 WebSocket 发送（串行化，避免并发 send 帧错乱）"""
        if self.websocket is None:
            return
        async with self._send_lock:
            await self.websocket.send(data)

    async def safe_send_bytes(self, data: bytes):
        """二进制帧安全发送（intercom 透传用）。与 safe_send 共用同一把 _send_lock。"""
        if self.websocket is None:
            return
        async with self._send_lock:
            await self.websocket.send(data)

    async def close(self, ws=None):
        """资源清理方法"""
        self._server_close = True
        try:
            if (
                    hasattr(self, "vad")
                    and self.vad
                    and hasattr(self.vad, "release_conn_resources")
            ):
                self.vad.release_conn_resources(self)

            if hasattr(self, "_connection_opus_decoder"):
                try:
                    delattr(self, "_connection_opus_decoder")
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"资源清理/上下线异常: {e}")

            if hasattr(self, "audio_buffer"):
                self.audio_buffer.clear()

            if self.timeout_task and not self.timeout_task.done():
                self.timeout_task.cancel()
                try:
                    await self.timeout_task
                except asyncio.CancelledError:
                    pass
                self.timeout_task = None

            if hasattr(self, "_aec_cache_cleanup_task") and self._aec_cache_cleanup_task and not self._aec_cache_cleanup_task.done():
                self._aec_cache_cleanup_task.cancel()
                try:
                    await self._aec_cache_cleanup_task
                except asyncio.CancelledError:
                    pass
                self._aec_cache_cleanup_task = None

            if hasattr(self, "_text_stream_task") and self._text_stream_task and not self._text_stream_task.done():
                self._text_stream_task.cancel()
                try:
                    await self._text_stream_task
                except asyncio.CancelledError:
                    pass
                self._text_stream_task = None

            _vt = getattr(self, "vad_resume_task", None)
            if _vt is not None and not _vt.done():
                _vt.cancel()
                try:
                    await _vt
                except asyncio.CancelledError:
                    pass

            if hasattr(self, "aec_audio_cache"):
                self.aec_audio_cache.clear()
                self.aec_audio_cache_time.clear()

            if hasattr(self, "func_handler") and self.func_handler:
                try:
                    await self.func_handler.cleanup()
                except Exception as cleanup_error:
                    self.logger.bind(tag=TAG).error(
                        f"清理工具处理器时出错: {cleanup_error}"
                    )

            if self.stop_event:
                self.stop_event.set()

            self.clear_queues()

            try:
                if ws:
                    try:
                        if hasattr(ws, "closed") and not ws.closed:
                            await ws.close()
                        elif hasattr(ws, "state") and ws.state.name != "CLOSED":
                            await ws.close()
                        else:
                            await ws.close()
                    except Exception:
                        pass
                elif self.websocket:
                    try:
                        if (
                                hasattr(self.websocket, "closed")
                                and not self.websocket.closed
                        ):
                            await self.websocket.close()
                        elif (
                                hasattr(self.websocket, "state")
                                and self.websocket.state.name != "CLOSED"
                        ):
                            await self.websocket.close()
                        else:
                            await self.websocket.close()
                    except Exception:
                        pass
            except Exception as ws_error:
                self.logger.bind(tag=TAG).error(f"关闭WebSocket连接时出错: {ws_error}")

            if self.tts:
                await self.tts.close()
            if self.asr:
                await self.asr.close()

            # executor 是进程级共享池，连接关闭只解引用、绝不 shutdown
            if self.executor is not None:
                if self.executor is not _SHARED_CHAT_EXECUTOR:
                    try:
                        self.executor.shutdown(wait=False)
                    except Exception as executor_error:
                        self.logger.bind(tag=TAG).error(
                            f"关闭线程池时出错: {executor_error}"
                        )
                self.executor = None
            self.logger.bind(tag=TAG).info("连接资源已释放")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"关闭连接时出错: {e}")
        finally:
            if self.stop_event:
                self.stop_event.set()

    def clear_queues(self):
        """清空所有任务队列"""
        if self.tts:
            self.logger.bind(tag=TAG).debug(
                f"开始清理: TTS队列大小={self.tts.tts_text_queue.qsize()}, 音频队列大小={self.tts.tts_audio_queue.qsize()}"
            )

            for q in [
                self.tts.tts_text_queue,
                self.tts.tts_audio_queue,
                self.report_queue,
            ]:
                if not q:
                    continue
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break

            if hasattr(self, "audio_rate_controller") and self.audio_rate_controller:
                self.audio_rate_controller.reset()
                self.logger.bind(tag=TAG).debug("已重置音频流控器")

            self.logger.bind(tag=TAG).debug(
                f"清理结束: TTS队列大小={self.tts.tts_text_queue.qsize()}, 音频队列大小={self.tts.tts_audio_queue.qsize()}"
            )

    def reset_audio_states(self):
        """重置本轮音频相关状态（VAD + ASR + AEC/barge-in）。"""
        self.client_audio_buffer.clear()
        self.client_have_voice = False
        self.client_voice_stop = False
        self.client_voice_window.clear()
        self.last_is_voice = False
        self.vad_last_voice_time = 0.0

        # Reset VAD ONNX hidden states（防止长时间运行后状态漂移导致端点检测失效）
        if hasattr(self, "_vad_state"):
            self._vad_state = np.zeros((2, 1, 128), dtype=np.float32)
        if hasattr(self, "_vad_context"):
            self._vad_context = np.zeros((1, 64), dtype=np.float32)
        if hasattr(self, "_vad_speech_start_time"):
            self._vad_speech_start_time = 0.0

        self.asr_audio.clear()

        # Reset AEC / barge-in 状态（abort 后干净重启，避免跨轮钉死旧回声基线）
        self._aec_last_effective = False
        self._raw_mic_rms = 0.0
        self._echo_baseline_rms = 0.0
        self._baseline_ready = False
        self._aec_eff_streak = 0

        self.logger.bind(tag=TAG).debug("All audio states reset.")

    def chat_and_close(self, text):
        """Chat with the user and then close the connection"""
        try:
            self.chat(text)
            self.close_after_chat = True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Chat and close error: {str(e)}")

    async def note_malformed_message(self, reason: str, message) -> bool:
        """登记一条畸形消息（未知 type / 非 JSON），必要时主动关闭连接。

        返回 True 表示已触发关闭（调用方应立即 return）。绝不进入 chat()/TTS 队列。
        """
        if not self._mg_enable:
            return False
        now = time.time()
        self._malformed_total += 1
        self._malformed_ts.append(now)
        window_start = now - self._mg_window_sec
        window_count = sum(1 for t in self._malformed_ts if t >= window_start)

        over_total = self._malformed_total > self._mg_max_total
        over_window = window_count > self._mg_window_max
        if not (over_total or over_window):
            return False

        try:
            sample = str(message)
            if len(sample) > 120:
                sample = sample[:120] + "..."
        except Exception:
            sample = "<unrepr>"
        self.logger.bind(tag=TAG).warning(
            f"[畸形连接防护] 主动关闭连接 device={self.device_id} reason={reason} "
            f"total={self._malformed_total} window={window_count}/{self._mg_window_sec}s "
            f"hello={self._hello_received} sample={sample}"
        )
        try:
            self.stop_event.set()
            await self.close(self.websocket)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"[畸形连接防护] 关闭连接出错: {e}")
        return True

    async def _check_timeout(self):
        """检查连接超时"""
        try:
            while not self.stop_event.is_set():
                # 握手超时——已收到过消息但迟迟未完成合法 hello，判定为畸形客户端并关闭。
                if (
                    self._mg_enable
                    and not self._hello_received
                    and not self.conn_from_mqtt_gateway
                    and self.bind_completed_event.is_set()
                    and not self.need_bind
                    and self._first_msg_ts > 0.0
                    and (time.time() * 1000 - self._first_msg_ts)
                    > self._mg_handshake_sec * 1000
                ):
                    if not self.stop_event.is_set():
                        self.logger.bind(tag=TAG).warning(
                            f"[畸形连接防护] 握手超时未收到合法 hello，关闭连接 "
                            f"device={self.device_id}"
                        )
                        self.stop_event.set()
                        try:
                            await self.close(self.websocket)
                        except Exception as close_error:
                            self.logger.bind(tag=TAG).error(
                                f"握手超时关闭连接时出错: {close_error}"
                            )
                    break

                last_activity_time = self.last_activity_time
                if self.need_bind:
                    last_activity_time = self.first_activity_time

                if last_activity_time > 0.0:
                    current_time = time.time() * 1000
                    if current_time - last_activity_time > self.timeout_seconds * 1000:
                        if not self.stop_event.is_set():
                            # 应用层静默超时，但 TCP 可能仍存活——先以传输层 ping 探活：
                            # 探活成功（TCP 仍通）仅应用层空闲，刷新活动时间续命；
                            # 探活失败（连接确已死）才真正关闭。未绑定连接保持原行为直接关闭。
                            closed = False
                            if not self.need_bind:
                                try:
                                    await asyncio.wait_for(self.websocket.ping(), timeout=5.0)
                                    self.last_activity_time = time.time() * 1000
                                    self.logger.bind(tag=TAG).debug(
                                        f"[存活探测] 静默超时豁免 device={self.device_id}，TCP 仍在线"
                                    )
                                except Exception as probe_err:
                                    self.logger.bind(tag=TAG).info(
                                        f"连接超时，准备关闭（探活失败: {probe_err}）"
                                    )
                                    self.stop_event.set()
                                    try:
                                        await self.close(self.websocket)
                                    except Exception as close_error:
                                        self.logger.bind(tag=TAG).error(
                                            f"超时关闭连接时出错: {close_error}"
                                        )
                                    closed = True
                            else:
                                self.logger.bind(tag=TAG).info("连接超时，准备关闭")
                                self.stop_event.set()
                                try:
                                    await self.close(self.websocket)
                                except Exception as close_error:
                                    self.logger.bind(tag=TAG).error(
                                        f"超时关闭连接时出错: {close_error}"
                                    )
                                closed = True
                            if closed:
                                break
                # 每 10 秒检查一次
                await asyncio.sleep(10)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"超时检查任务出错: {e}")
        finally:
            self.logger.bind(tag=TAG).info("超时检查任务已退出")

    async def _check_aec_cache_expiry(self):
        """定期清理过期的 AEC 缓存"""
        try:
            while not self.stop_event.is_set():
                if hasattr(self, "aec_audio_cache") and self.aec_audio_cache:
                    current_time = time.time()
                    expired_keys = [
                        ts for ts, cache_time in list(self.aec_audio_cache_time.items())
                        if current_time - cache_time > 120
                    ]
                    for ts in expired_keys:
                        self.aec_audio_cache.pop(ts, None)
                        self.aec_audio_cache_time.pop(ts, None)
                    if expired_keys:
                        self.logger.bind(tag=TAG).debug(f"[AEC] 清理过期缓存 {len(expired_keys)} 条")
                await asyncio.sleep(30)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"AEC缓存清理任务出错: {e}")

    def _extract_direct_answer_response(self, arguments_str):
        """从 direct_answer 的参数中提取 response 值。优先 json.loads，流式阶段 fallback 字符串提取。"""
        if not arguments_str:
            return ""
        try:
            data = json.loads(arguments_str)
            if isinstance(data, dict) and "response" in data:
                return data["response"]
        except (json.JSONDecodeError, TypeError):
            pass
        marker = '"response": "'
        idx = arguments_str.find(marker)
        if idx < 0:
            marker = '"response":"'
            idx = arguments_str.find(marker)
        if idx < 0:
            return ""
        start = idx + len(marker)
        raw = arguments_str[start:]
        if raw.endswith('"}'):
            raw = raw[:-2]
        elif raw.endswith('"'):
            raw = raw[:-1]
        raw = raw.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
        return raw

    @staticmethod
    def _clean_response_garbage(text):
        """清理 response 中可能泄漏的 JSON 闭合符号。"""
        if not text:
            return text
        _garbage_chars = frozenset('")\'}）')
        lines = text.split('\n')
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if stripped and len(stripped) <= 8 and all(c in _garbage_chars for c in stripped):
                continue
            cleaned.append(line)
        result = '\n'.join(cleaned)
        result = re.sub(r'["\'}\]]+$', '', result.rstrip()).rstrip()
        return result

    def _merge_tool_calls(self, tool_calls_list, tools_call):
        """合并工具调用列表"""
        for tool_call in tools_call:
            tool_index = getattr(tool_call, "index", None)
            if tool_index is None:
                if tool_call.function.name:
                    tool_index = len(tool_calls_list)
                else:
                    tool_index = len(tool_calls_list) - 1 if tool_calls_list else 0

            if tool_index >= len(tool_calls_list):
                tool_calls_list.append({"id": "", "name": "", "arguments": ""})

            if tool_call.id:
                tool_calls_list[tool_index]["id"] = tool_call.id
            if tool_call.function.name:
                tool_calls_list[tool_index]["name"] = tool_call.function.name
            if tool_call.function.arguments:
                tool_calls_list[tool_index]["arguments"] += tool_call.function.arguments
