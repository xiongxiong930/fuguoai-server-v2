"""复果品牌知识库检索工具（search_brand_kb）。

从本地 BrandKB 检索关于复果AI / 复果造物工坊 / 复果产品 / 门店 / 服务的资料，
取回片段后拼成引用块交给主 LLM 合成。对外契约（工具名、参数 schema、Action/ActionResponse）
保持稳定，与 search_from_ragflow 同范式。
"""

import json
from typing import TYPE_CHECKING

from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import register_function, ToolType, ActionResponse, Action

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

SEARCH_BRAND_KB_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "search_brand_kb",
        "description": (
            "从复果自有品牌知识库中查询关于「复果AI、复果造物工坊、复果产品、门店、"
            "服务、售后、与微信小程序关系」的资料。当用户询问复果相关业务、品牌介绍、"
            "产品信息、造物工坊、购买方式、门店位置时优先使用此工具，不要凭空编造。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "关于复果品牌/产品/服务的查询问题"}},
            "required": ["question"],
        },
    },
}


def _clean_question(question) -> str:
    if question and isinstance(question, str):
        return question
    return str(question) if question is not None else ""


_MAX_CHUNKS = 5


@register_function("search_brand_kb", SEARCH_BRAND_KB_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def search_brand_kb(conn: "object", question=None):
    """向本地 BrandKB 查询复果品牌知识并回传拼装好的引用块。"""
    question = _clean_question(question)

    # 延迟导入，避免与 connection / embedding 初始化形成循环依赖
    try:
        from fuguoai_core.utils.brand_kb import get_brand_kb
    except Exception as exc:
        logger.bind(tag=TAG).error(f"BrandKB 导入失败: {exc}")
        return ActionResponse(
            Action.REQLLM,
            "（复果品牌知识库模块加载失败）请根据你对复果AI的公开认知简要、如实回答；若涉及未公开的内部信息，坦诚说明不知道。",
            None,
        )

    bkb = get_brand_kb(getattr(conn, "config", None) or {})
    if not bkb.enabled:
        return ActionResponse(
            Action.REQLLM,
            "（复果品牌知识库当前未配置）请根据你对复果AI的公开认知，简要、如实地回答；若涉及未公开的内部信息，请坦诚说明你不知道，不要编造。",
            None,
        )

    docs = bkb.retrieve(question)
    if not docs:
        return ActionResponse(
            Action.REQLLM,
            "用户问的是复果（fuguoai）相关业务，但复果品牌知识库暂无匹配内容。请根据你对复果AI的公开认知简要、如实回答；若确属内部未公开信息，坦诚说明不知道，不要编造。",
            None,
        )

    chunks = []
    for d in docs[:_MAX_CHUNKS]:
        title = d.get("title", "")
        text = d.get("text", "")
        chunks.append(f"{title}\n{text}" if title else text)
    text = "# 关于复果品牌知识（来源：复果品牌知识库）\n```\n\n\n".join(chunks)
    return ActionResponse(Action.REQLLM, text, None)
