"""RAGFlow 知识库检索工具（复果自有实现）。

把用户问题发给 RAGFlow retrieval 接口，取回片段后拼成给 LLM 的引用块。对外契约
（工具名、参数 schema、Action/ActionResponse 与错误文案）保持稳定。
"""

import json
import httpx
from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

SEARCH_FROM_RAGFLOW_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "search_from_ragflow",
        "description": "从知识库中查询信息",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "查询的问题"}},
            "required": ["question"],
        },
    },
}

# 最多引用多少个知识库片段
_MAX_CHUNKS = 5


def _clean_question(question) -> str:
    """把问题统一成字符串。"""
    if question and isinstance(question, str):
        return question
    return str(question) if question is not None else ""


def _format_api_error(result: dict) -> str:
    """RAG 接口业务码非 0 时的错误文案。"""
    error = result.get("error", {}) or {}
    detail = error.get("detail", "未知错误")
    message = error.get("message", "")
    code = result.get("code", "")

    logger.bind(tag=TAG).error(
        f"RAGFlow API调用失败，响应码：{code}，错误详情：{detail}，完整响应：{result}"
    )

    text = f"RAG接口返回异常（错误码：{code}）"
    if message:
        text += f"：{message}"
    if detail:
        text += f"\n详情：{detail}"
    return text


def _extract_chunks(result: dict) -> list:
    """从检索结果里挑出可用的正文片段。"""
    chunks = []
    for piece in result.get("data", {}).get("chunks", []):
        content = piece.get("content", "")
        if not content:
            continue
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, bytes):
            chunks.append(content.decode("utf-8", errors="replace"))
        else:
            chunks.append(str(content))
    return chunks


def _assemble_context(question: str, chunks: list) -> str:
    """把知识库片段拼成给模型看的引用块。"""
    if not chunks:
        return "根据知识库查询结果，没有相关信息。"
    text = f"# 关于问题【{question}】查到知识库如下\n"
    text += "```\n\n\n".join(chunks[:_MAX_CHUNKS])
    text += "\n```"
    return text


@register_function(
    "search_from_ragflow", SEARCH_FROM_RAGFLOW_FUNCTION_DESC, ToolType.SYSTEM_CTL
)
async def search_from_ragflow(conn: "ConnectionHandler", question=None):
    """向 RAGFlow 查询知识库并回传拼装好的引用块。"""
    question = _clean_question(question)

    rag_cfg = conn.config.get("plugins", {}).get("search_from_ragflow", {})
    base_url = rag_cfg.get("base_url", "")
    api_key = rag_cfg.get("api_key", "")
    dataset_ids = rag_cfg.get("dataset_ids", [])

    url = base_url + "/api/v1/retrieval"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"question": question, "dataset_ids": dataset_ids}

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, connect=3.0), verify=False
        ) as client:
            reply = await client.post(url, json=payload, headers=headers)

        # 显式指定编码，避免中文被按 latin-1 解读
        reply.encoding = "utf-8"
        reply.raise_for_status()
        result = json.loads(reply.text)

        if result.get("code") != 0:
            return ActionResponse(Action.RESPONSE, None, _format_api_error(result))

        return ActionResponse(
            Action.REQLLM, _assemble_context(question, _extract_chunks(result)), None
        )

    except httpx.TimeoutException:
        return ActionResponse(
            Action.RESPONSE,
            None,
            "RAG接口请求超时\n可能原因：RAGflow服务响应缓慢或网络延迟\n解决方案：请稍后重试或检查RAGflow服务性能",
        )

    except httpx.HTTPStatusError as exc:
        if hasattr(exc.response, "status_code"):
            status = exc.response.status_code
            text = f"RAG接口HTTP错误（状态码：{status}）"
            try:
                detail = exc.response.json().get("error", {}).get("message", "")
                if detail:
                    text += f"\n错误详情：{detail}"
            except Exception:
                pass
        else:
            text = f"RAG接口HTTP异常：{str(exc)}"
        return ActionResponse(Action.RESPONSE, None, text)

    except httpx.HTTPError:
        return ActionResponse(
            Action.RESPONSE,
            None,
            "无法连接到RAG接口\n可能原因：RAGflow服务地址错误或服务未运行\n解决方案：请检查RAGflow服务地址配置和服务状态",
        )

    except Exception as exc:
        error_type = type(exc).__name__
        logger.bind(tag=TAG).error(
            f"RAGflow处理异常，异常类型：{error_type}，详情：{str(exc)}"
        )
        return ActionResponse(
            Action.RESPONSE, None, f"RAG接口处理异常（{error_type}）：{str(exc)}"
        )
