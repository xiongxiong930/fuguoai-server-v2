"""文旅知识库 HTTP 路由（TourismKB API）。

路由前缀 /api/v1/kb，由 fuguoai_core.api.registry.apply_routes 自动发现并挂载
（fail-soft：本模块异常不影响核心路由）。无需改动 http_server.py 的巨型 add_routes。
"""

import asyncio

from aiohttp import web

from fuguoai_config.logger import setup_logging
from fuguoai_core.api.registry import register_routes
from fuguoai_core.utils.tourism_kb import get_tourism_kb

logger = setup_logging()


@register_routes
def register_kb_routes(app):
    app.router.add_post("/api/v1/kb/query", _handle_kb_query)
    app.router.add_get("/api/v1/kb/{kb_id}/docs", _handle_kb_list)
    app.on_startup.append(_warmup_on_startup)


async def _warmup_on_startup(app):
    """启动预热（默认关闭，见 TourismKB.warmup.enabled）。

    实测：进程重启后首个文旅请求约 390ms（embedding 模型加载 352ms + 建索引 24ms + 数字索引 5.5ms），
    之后 5ms 量级。开启预热即把这段挪到启动阶段；预热是纯建缓存，不改变任何检索结果。
    跑在 executor 里（阻塞 400ms 级，不能堵住事件循环）；任何异常只记日志，不影响服务启动。
    """
    loop = asyncio.get_event_loop()

    def _do():
        kb = get_tourism_kb()
        if kb is None or not kb.enabled or not getattr(kb, "warmup_enabled", False):
            return None
        return kb.warmup()

    try:
        res = await loop.run_in_executor(None, _do)
    except Exception as e:
        logger.bind(tag=__name__).warning(f"文旅 KB 预热跳过: {e}")
        return
    if res:
        logger.bind(tag=__name__).info(f"文旅 KB 启动预热: {res}")


async def _handle_kb_query(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    kb_id = (body.get("kb_id") or "hubei").strip()
    question = (body.get("question") or "").strip()
    if not question:
        return web.json_response({"error": "question required"}, status=400)

    kb = get_tourism_kb()
    if kb is None or not kb.enabled:
        return web.json_response({"error": "tourism kb disabled"}, status=503)

    try:
        res = kb.query(kb_id, question, use_llm=True)
    except Exception as e:
        return web.json_response({"error": f"query failed: {e}"}, status=500)
    return web.json_response(res)


async def _handle_kb_list(request):
    kb_id = request.match_info.get("kb_id", "hubei")
    kb = get_tourism_kb()
    if kb is None or not kb.enabled:
        return web.json_response({"error": "tourism kb disabled"}, status=503)
    try:
        docs = kb.list_docs(kb_id)
    except Exception as e:
        return web.json_response({"error": f"list failed: {e}"}, status=500)
    return web.json_response({"kb_id": kb_id, "count": len(docs), "docs": docs})
