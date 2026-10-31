"""意图识别与分派（复果 AI 自有实现 · clean-room 重写）。

本模块固定「用户输入 → 规则短路 → LLM 意图分析 → 工具调用 / 上下文问答」主链路：
明确的退出命令、闭嘴/停止说话、唤醒词命中即短路，不再走 LLM 常规应答；
其余情况交 LLM 判定意图，再分派到工具调用或带上下文问答。

所有会被播报出去、或写入对话历史的文本（提示词模板、超时兜底话术、结果上下文
提示）均逐字固定，不随表达调整而变动。
"""

import json
import uuid
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
from fuguoai_core.utils.dialogue import Message
from fuguoai_core.providers.tts.dto.dto import ContentType
from fuguoai_core.handle.helloHandle import checkWakeupWords
from fuguoai_core.handle.abortHandle import handleAbortMessage
from fuguoai_plugins.register import Action, ActionResponse
from fuguoai_core.handle.sendAudioHandle import send_stt_message
from fuguoai_core.handle.reportHandle import enqueue_tool_report
from fuguoai_core.utils.util import remove_punctuation_and_length
from fuguoai_core.providers.tts.dto.dto import TTSMessageDTO, SentenceType

TAG = __name__

# 命中即停播报、不再搭话、保持连接的默认触发词。
_STOP_TALKING_KEYWORDS = (
    "闭嘴", "别吵", "不要吵", "吵死", "下去", "安静点",
    "别说话", "停止说话", "别bb", "shut up", "闭嘴吧", "你闭嘴",
)

# 工具调用默认超时（秒）。
_TOOL_CALL_TIMEOUT = 30
# 工具调用失败时的兜底播报。
_TOOL_CALL_TIMEOUT_HINT = "工具调用超时，请一会再试下哈"


async def handle_user_intent(conn: "ConnectionHandler", text):
    """意图总入口。返回 True 表示已在模块内处理完，无需再走常规 LLM 应答。"""
    text = _unwrap_json_input(text, conn)

    cleaned = remove_punctuation_and_length(text)[1]
    if await _match_exit(conn, cleaned):
        return True
    if await _match_stop_talking(conn, cleaned):
        return True
    if await checkWakeupWords(conn, cleaned):
        return True

    # function_call 模式下由模型直接产出工具调用，不再做显式意图分析。
    if conn.intent_type == "function_call":
        return False

    intent_payload = await _detect_intent(conn, text)
    if not intent_payload:
        return False

    conn.sentence_id = str(uuid.uuid4().hex)
    return await _route_intent(conn, intent_payload, text)


def _unwrap_json_input(text, conn):
    """输入可能是 {content:..., speaker:...} 形态的 JSON，提取正文并保留说话人。"""
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return text
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(payload, dict) and "content" in payload:
        conn.current_speaker = payload.get("speaker")
        return payload["content"]
    return text


async def _match_exit(conn, text):
    """明确的退出命令：播报原句并关闭连接。"""
    normalized = remove_punctuation_and_length(text)[1]
    for command in conn.cmd_exit:
        if normalized == command:
            conn.logger.bind(tag=TAG).info(f"识别到明确的退出命令: {text}")
            await send_stt_message(conn, text)
            await conn.close()
            return True
    return False


async def _match_stop_talking(conn, text):
    """用户要求停播报：停当前 TTS，不生成新回复，保持连接。

    用于消解"用户说不要吵了 / 下去，AI 反而继续搭话"的体验硬伤。
    """
    normalized = remove_punctuation_and_length(text)[1]
    keywords = getattr(conn, "cmd_stop_talking", _STOP_TALKING_KEYWORDS)
    for keyword in keywords:
        if keyword and keyword in normalized:
            conn.logger.bind(tag=TAG).info(f"识别到闭嘴/停止指令: {normalized}")
            await handleAbortMessage(conn)
            return True
    return False


async def _detect_intent(conn, text):
    """用 LLM 做意图分析；未初始化或服务异常时返回 None。"""
    intent = getattr(conn, "intent", None)
    if not intent:
        conn.logger.bind(tag=TAG).warning("意图识别服务未初始化")
        return None
    try:
        return await conn.intent.detect_intent(conn, conn.dialogue.dialogue, text)
    except Exception as exc:
        conn.logger.bind(tag=TAG).error(f"意图识别失败: {exc}")
        return None


def _normalize_arguments(raw_args):
    """把意图结果里的 function_call.arguments 规整为字符串形态。"""
    if raw_args is None:
        raw_args = {}
    if isinstance(raw_args, dict):
        return json.dumps(raw_args)
    return raw_args


async def _route_intent(conn, intent_payload, original_text):
    """解析 LLM 意图结果并分派到上下文问答或工具调用。"""
    try:
        intent_data = json.loads(intent_payload)
    except json.JSONDecodeError as exc:
        conn.logger.bind(tag=TAG).error(f"处理意图结果时出错: {exc}")
        return False

    if "function_call" not in intent_data:
        return False

    function_name = intent_data["function_call"]["name"]
    if function_name == "continue_chat":
        return False

    call = {
        "name": function_name,
        "id": str(uuid.uuid4().hex),
        "arguments": _normalize_arguments(
            intent_data["function_call"].get("arguments")
        ),
    }

    if function_name == "result_for_context":
        await _reply_with_context(conn, original_text)
        return True

    await _invoke_tool(conn, function_name, call, original_text)
    return True


async def _reply_with_context(conn, original_text):
    """result_for_context：把原句送 LLM 带时间上下文作答。"""
    await send_stt_message(conn, original_text)
    conn.client_abort = False

    def task():
        conn.dialogue.put(Message(role="user", content=original_text))
        from fuguoai_core.utils.current_time import get_current_time_info

        current_time, today_date, today_weekday, lunar_date = get_current_time_info()
        prompt = (
            f"当前时间：{current_time}\n"
            f"今天日期：{today_date} ({today_weekday})\n"
            f"今天农历：{lunar_date}\n\n"
            f"请根据以上信息回答用户的问题：{original_text}"
        )
        _speak(conn, conn.intent.replyResult(prompt, original_text))

    conn.executor.submit(task)


async def _invoke_tool(conn, function_name, call, original_text):
    """工具调用：上报 + 在线程池执行 + 按结果动作播报。"""
    await send_stt_message(conn, original_text)
    conn.client_abort = False

    tool_input = {}
    arguments = call["arguments"]
    if arguments:
        tool_input = json.loads(arguments) if isinstance(arguments, str) else arguments

    enqueue_tool_report(conn, function_name, tool_input)

    def task():
        conn.dialogue.put(Message(role="user", content=original_text))
        timeout = int(conn.config.get("tool_call_timeout", _TOOL_CALL_TIMEOUT))
        try:
            result = asyncio.run_coroutine_threadsafe(
                conn.func_handler.handle_llm_function_call(conn, call),
                conn.loop,
            ).result(timeout=timeout)
        except Exception as exc:
            conn.logger.bind(tag=TAG).error(f"工具调用失败: {exc}")
            result = ActionResponse(
                action=Action.ERROR,
                result=_TOOL_CALL_TIMEOUT_HINT,
                response=_TOOL_CALL_TIMEOUT_HINT,
            )

        if not result:
            return
        enqueue_tool_report(
            conn,
            function_name,
            tool_input,
            str(result.result) if result.result else None,
            report_tool_call=False,
        )

        if result.action == Action.RESPONSE:
            if result.response is not None:
                _speak(conn, result.response)
        elif result.action == Action.REQLLM:
            conn.dialogue.put(Message(role="tool", content=result.result))
            reply = conn.intent.replyResult(result.result, original_text)
            _speak(conn, result.result if reply is None else reply)
        elif result.action in (Action.NOTFOUND, Action.ERROR):
            text = result.response if result.response else result.result
            if text is not None:
                _speak(conn, text)
        elif function_name != "play_music":
            # 兼容旧版未显式声明动作的回调
            text = result.response if result.response is not None else result.result
            if text is not None:
                _speak(conn, text)

    conn.executor.submit(task)


def _speak(conn: "ConnectionHandler", text):
    """把一段文本按 FIRST→正文→LAST 的队列顺序送进 TTS，并写入对话历史。"""
    conn.tts.store_tts_text(conn.sentence_id, text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )
    conn.dialogue.put(Message(role="assistant", content=text))
