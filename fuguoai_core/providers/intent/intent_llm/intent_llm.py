# -*- coding: utf-8 -*-
"""复果AI · 外挂 LLM 意图识别 Provider

用一个独立（通常更小更快）的 LLM 先行判定用户意图，输出 function_call
结构的 JSON，交由 intentHandler 决定走工具、上下文直答还是普通聊天。

要点
----
- 系统提示词在首轮惰性构建并缓存在实例上（含设备函数 + MCP 工具清单）；
  每轮再动态追加音乐曲库与 Home Assistant 设备清单。
- 结果按 md5(device_id + text) 做进程级缓存，重复口令零 LLM 开销。
- 任何 LLM/解析异常一律降级为 continue_chat，绝不阻断对话主链路。

自研演进（2026-07-29）
--------------------
1. LLM 网络调用经 asyncio.to_thread 下线程池：本服务为单事件循环架构，
   同步调用裸跑在 loop 上会在意图识别期间冻结全进程 WS 收发（含对讲
   relay 的 20ms 节奏）——上游遗留缺陷，此处修复。
2. 超时熔断（llm_timeout，默认 8s）：慢模型不再拖垮整轮对话，超时降级
   continue_chat。
3. 确定性快路径：时间/日期/农历类高频基础查询精确命中直返
   result_for_context，省一次 LLM 往返（数百毫秒~秒级）与调用成本。

⚠️ 提示词正文经真机调参锁定，改动措辞会漂移意图判定，勿随意增删。
"""

import asyncio
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
import time
from typing import Dict, List, Optional, TYPE_CHECKING

from fuguoai_config.logger import setup_logging
from fuguoai_core.utils.util import get_system_error_response
from fuguoai_plugins.functions.play_music import initialize_music_handler

from ..base import IntentProviderBase

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 意图识别专属有界线程池：与 loop 默认池、ASR 池隔离，避免慢模型意图请求
# 占满默认池而拖垮其他下沉任务（跨设备干扰）。
_INTENT_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="intent")

# 兜底意图：任何异常路径都回落到普通聊天
_FALLBACK = '{"function_call": {"name": "continue_chat"}}'

# 上下文直答意图（时间/日期/农历类基础查询）
_CONTEXT_ANSWER = '{"function_call": {"name": "result_for_context"}}'

# ── 确定性快路径 ─────────────────────────────────────────────────────────
# 高频基础查询精确命中时跳过 LLM 往返（节省数百毫秒~秒级时延与一次调用成本）。
# 语义上与提示词【重要规则】的 result_for_context 分支一一对应，只做"确定
# 无歧义"的全匹配，任何拿不准的输入都照走 LLM，不改变判定行为。
_FAST_CONTEXT_QUERIES = frozenset({
    # 时间
    "现在几点", "现在几点了", "几点了", "现在几点钟", "当前时间", "查询时间",
    "现在是几点", "现在时间", "报时",
    # 日期
    "今天几号", "今天几号了", "今天是几号", "今天星期几", "今天周几",
    "今天是星期几", "今天是什么日期", "今天日期", "今天是几月几号",
    # 农历/节气
    "今天农历几号", "今天是农历几号", "今天农历", "今天什么节气", "今天是什么节气",
})

_PUNCT_STRIP = re.compile(r"[\s，。？！?!,.、；;：:\"'“”‘’~～]")


def _fast_context_route(text: str) -> Optional[str]:
    """确定性意图路由：精确命中基础查询返回 result_for_context，否则 None。"""
    normalized = _PUNCT_STRIP.sub("", text or "")
    if normalized in _FAST_CONTEXT_QUERIES:
        return _CONTEXT_ANSWER
    return None

# ── 系统提示词（运行期字节与调参基线一致，仅源码排版归我方） ──────────────
_PROMPT_HEAD = (
    "【严格格式要求】你必须只能返回JSON格式，绝对不能返回任何自然语言！\n\n"
    "你是一个意图识别助手。请分析用户的最后一句话，判断用户意图并调用相应的函数。\n\n"
    "【重要规则】以下类型的查询请直接返回result_for_context，无需调用函数：\n"
    "- 询问当前时间（如：现在几点、当前时间、查询时间等）\n"
    "- 询问今天日期（如：今天几号、今天星期几、今天是什么日期等）\n"
    "- 询问今天农历（如：今天农历几号、今天什么节气等）\n"
    "- 询问所在城市（如：我现在在哪里、你知道我在哪个城市吗等）"
    "系统会根据上下文信息直接构建回答。\n\n"
    "- 如果用户使用疑问词（如'怎么'、'为什么'、'如何'）询问退出相关的问题"
    "（例如'怎么退出了？'），注意这不是让你退出，请返回 "
    "{'function_call': {'name': 'continue_chat'}\n"
    "- 仅当用户明确使用'退出系统'、'结束对话'、'我不想和你说话了'等指令时，"
    "才触发 handle_exit_intent\n\n"
)

_PROMPT_TAIL = (
    "处理步骤:\n"
    "1. 分析用户输入，确定用户意图\n"
    "2. 检查是否为上述基础信息查询（时间、日期等），如是则返回result_for_context\n"
    "3. 从可用函数列表中选择最匹配的函数\n"
    "4. 如果找到匹配的函数，生成对应的function_call 格式\n"
    '5. 如果没有找到匹配的函数，返回{"function_call": {"name": "continue_chat"}}\n\n'
    "返回格式要求：\n"
    "1. 必须返回纯JSON格式，不要包含任何其他文字\n"
    "2. 必须包含function_call字段\n"
    "3. function_call必须包含name字段\n"
    "4. 如果函数需要参数，必须包含arguments字段\n\n"
    "示例：\n"
    "```\n"
    "用户: 现在几点了？\n"
    '返回: {"function_call": {"name": "result_for_context"}}\n'
    "```\n"
    "```\n"
    "用户: 当前电池电量是多少？\n"
    '返回: {"function_call": {"name": "get_battery_level", "arguments": '
    '{"response_success": "当前电池电量为{value}%", "response_failure": '
    '"无法获取Battery的当前电量百分比"}}}\n'
    "```\n"
    "```\n"
    "用户: 当前屏幕亮度是多少？\n"
    '返回: {"function_call": {"name": "self_screen_get_brightness"}}\n'
    "```\n"
    "```\n"
    "用户: 设置屏幕亮度为50%\n"
    '返回: {"function_call": {"name": "self_screen_set_brightness", '
    '"arguments": {"brightness": 50}}}\n'
    "```\n"
    "```\n"
    "用户: 我想结束对话\n"
    '返回: {"function_call": {"name": "handle_exit_intent", "arguments": '
    '{"say_goodbye": "goodbye"}}}\n'
    "```\n"
    "```\n"
    "用户: 你好啊\n"
    '返回: {"function_call": {"name": "continue_chat"}}\n'
    "```\n\n"
    "注意：\n"
    "1. 只返回JSON格式，不要包含任何其他文字\n"
    "2. 优先检查用户查询是否为基础信息（时间、日期等），如是则返回"
    '{"function_call": {"name": "result_for_context"}}，不需要arguments参数\n'
    '3. 如果没有找到匹配的函数，返回{"function_call": {"name": "continue_chat"}}\n'
    "4. 确保返回的JSON格式正确，包含所有必要的字段\n"
    "5. result_for_context不需要任何参数，系统会自动从上下文获取信息\n"
    "特殊说明：\n"
    "- 当用户单次输入包含多个指令时（如'打开灯并且调高音量'）\n"
    "- 请返回多个function_call组成的JSON数组\n"
    "- 示例：{'function_calls': [{name:'light_on'}, {name:'volume_up'}]}\n\n"
    "【最终警告】绝对禁止输出任何自然语言、表情符号或解释文字！"
    "只能输出有效JSON格式！违反此规则将导致系统错误！"
)


def _render_function_catalog(functions_list) -> str:
    """把设备函数/MCP 工具清单渲染为提示词里的函数说明段。"""
    lines = ["可用的函数列表：\n"]
    for entry in functions_list:
        meta = entry.get("function", {})
        lines.append(f"\n函数名: {meta.get('name', '')}\n")
        lines.append(f"描述: {meta.get('description', '')}\n")
        schema = meta.get("parameters", {})
        if schema:
            lines.append("参数:\n")
            for arg_name, arg_meta in schema.get("properties", {}).items():
                lines.append(
                    f"- {arg_name} ({arg_meta.get('type', '')}): "
                    f"{arg_meta.get('description', '')}\n"
                )
        lines.append("---\n")
    return "".join(lines)


class IntentProvider(IntentProviderBase):
    # 携带进提示词的对话历史窗口（最近 N 条）
    _HISTORY_WINDOW = 4
    # LLM 意图判定超时（秒）：超过即降级 continue_chat，绝不拖垮整轮对话
    _DEFAULT_LLM_TIMEOUT = 8.0

    def __init__(self, config):
        super().__init__(config)
        from fuguoai_core.utils.cache.manager import CacheType, cache_manager

        self.llm = None
        self.promot = ""  # 惰性构建的系统提示词（首轮据函数清单生成后固定）
        self.cache_manager = cache_manager
        self.CacheType = CacheType
        self.history_count = self._HISTORY_WINDOW
        try:
            self.llm_timeout = float(
                (config or {}).get("llm_timeout", self._DEFAULT_LLM_TIMEOUT)
            )
        except (TypeError, ValueError):
            self.llm_timeout = self._DEFAULT_LLM_TIMEOUT

    def get_intent_system_prompt(self, functions_list: str) -> str:
        """按当前可用函数清单拼装完整系统提示词。"""
        return _PROMPT_HEAD + _render_function_catalog(functions_list) + "\n" + _PROMPT_TAIL

    def replyResult(self, text: str, original_text: str):
        """基于上下文信息直答（result_for_context 路径由 intentHandler 调用）。"""
        try:
            return self.llm.response_no_stream(
                system_prompt=text,
                user_prompt="请根据以上内容，像人类一样说话的口吻回复用户，"
                "要求简洁，请直接返回结果。用户现在说：" + original_text,
            )
        except Exception as exc:
            logger.bind(tag=TAG).error(f"Error in generating reply result: {exc}")
            return get_system_error_response(self.config)

    # ── 内部拼装 ─────────────────────────────────────────────────────────

    def _ensure_base_prompt(self, conn) -> None:
        """首轮惰性构建系统提示词：设备函数 + MCP 工具合并进函数清单。"""
        if self.promot:
            return
        functions = conn.func_handler.get_functions()
        if hasattr(conn, "mcp_client"):
            extra = conn.mcp_client.get_available_tools()
            if extra:
                functions = (functions or []) + list(extra)
        self.promot = self.get_intent_system_prompt(functions)

    def _build_round_prompt(self, conn) -> str:
        """在基础提示词上追加本轮动态上下文：曲库 + Home Assistant 设备。"""
        music_names = initialize_music_handler(conn)["music_file_names"]
        prompt = f"{self.promot}\n<musicNames>{music_names}\n</musicNames>"

        hass_cfg = conn.config["plugins"].get("home_assistant")
        devices = hass_cfg.get("devices", []) if hass_cfg else []
        if devices:
            prompt += (
                "\n下面是我家智能设备列表（位置，设备名，entity_id），"
                "可以通过homeassistant控制\n"
            )
            prompt += "".join(d + "\n" for d in devices)
        return prompt

    def _build_dialogue_digest(self, dialogue_history: List[Dict], text: str) -> str:
        """取最近 N 条历史 + 本轮输入，拼装用户侧提示。"""
        window = dialogue_history[max(0, len(dialogue_history) - self.history_count):]
        digest = "".join(f"{m.role}: {m.content}\n" for m in window)
        digest += f"User: {text}\n"
        return f"current dialogue:\n{digest}"

    def _post_process(self, conn, intent: str, cache_key: str) -> str:
        """解析 LLM 输出并按意图类型做善后；失败降级 continue_chat。"""
        try:
            parsed = json.loads(intent)
        except json.JSONDecodeError:
            logger.bind(tag=TAG).error(f"无法解析意图JSON: {intent}")
            return _FALLBACK

        call = parsed.get("function_call")
        if call is not None:
            name = call.get("name")
            logger.bind(tag=TAG).info(
                f"llm 识别到意图: {name}, 参数: {call.get('arguments', {})}"
            )
            if name == "result_for_context":
                logger.bind(tag=TAG).info(
                    "检测到result_for_context意图，将使用上下文信息直接回答"
                )
            elif name == "continue_chat":
                # 普通聊天：清掉历史里的工具消息，避免污染后续上下文
                conn.dialogue.dialogue = [
                    m for m in conn.dialogue.dialogue
                    if m.role not in ("tool", "function")
                ]
            else:
                logger.bind(tag=TAG).info(f"检测到函数调用意图: {name}")

        self.cache_manager.set(self.CacheType.INTENT, cache_key, intent)
        return intent

    # ── 主入口 ───────────────────────────────────────────────────────────

    async def detect_intent(
        self, conn: "ConnectionHandler", dialogue_history: List[Dict], text: str
    ) -> str:
        if not self.llm:
            raise ValueError("LLM provider not set")
        if conn.func_handler is None:
            return _FALLBACK

        t0 = time.time()
        model_label = getattr(self.llm, "model_name", str(self.llm.__class__.__name__))

        # 确定性快路径：高频基础查询零 LLM 开销（毫秒级返回）
        fast = _fast_context_route(text)
        if fast is not None:
            logger.bind(tag=TAG).info(
                f"意图快路径命中: '{text}' -> result_for_context, "
                f"耗时: {(time.time() - t0) * 1000:.1f}ms (省一次LLM往返)"
            )
            return fast

        # 命中缓存直接返回（同设备同文本）
        cache_key = hashlib.md5((conn.device_id + text).encode()).hexdigest()
        cached = self.cache_manager.get(self.CacheType.INTENT, cache_key)
        if cached is not None:
            logger.bind(tag=TAG).debug(
                f"使用缓存的意图: {cache_key} -> {cached}, 耗时: {time.time() - t0:.4f}秒"
            )
            return cached

        self._ensure_base_prompt(conn)
        system_prompt = self._build_round_prompt(conn)
        user_prompt = self._build_dialogue_digest(dialogue_history, text)
        logger.bind(tag=TAG).debug(
            f"意图识别预处理耗时: {time.time() - t0:.4f}秒, 模型: {model_label}"
        )

        # LLM 网络调用下线程池：本进程是单事件循环架构（所有设备 WS、对讲
        # relay 共享一个 loop），同步 HTTP 若直接跑在 loop 里，一台设备做意图
        # 识别的每一毫秒都在冻结全进程收发。带超时熔断，慢响应降级不拖对话。
        t_llm = time.time()
        try:
            loop = asyncio.get_running_loop()
            intent = await asyncio.wait_for(
                loop.run_in_executor(
                    _INTENT_EXECUTOR,
                    lambda: self.llm.response_no_stream(
                        system_prompt=system_prompt, user_prompt=user_prompt
                    ),
                ),
                timeout=self.llm_timeout,
            )
        except asyncio.TimeoutError:
            logger.bind(tag=TAG).warning(
                f"意图识别LLM超时(>{self.llm_timeout}s, 模型: {model_label})，"
                f"降级 continue_chat 走普通对话"
            )
            return _FALLBACK
        except Exception as exc:
            logger.bind(tag=TAG).error(f"Error in intent detection LLM call: {exc}")
            return _FALLBACK

        # 剥离围栏/前后噪声，仅取首个 JSON 体
        intent = intent.strip()
        body = re.search(r"\{.*\}", intent, re.DOTALL)
        if body:
            intent = body.group(0)

        logger.bind(tag=TAG).debug(
            f"【意图识别性能】模型: {model_label}, 总耗时: {time.time() - t0:.4f}秒, "
            f"LLM调用: {time.time() - t_llm:.4f}秒, 查询: '{text[:20]}...'"
        )
        return self._post_process(conn, intent, cache_key)
