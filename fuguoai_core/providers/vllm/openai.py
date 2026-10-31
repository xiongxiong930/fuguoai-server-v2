"""OpenAI 兼容视觉推理后端（复果自有实现）。

对接任意暴露 OpenAI 兼容 ``/chat/completions`` 协议的视觉推理服务
（vLLM、LM Studio、本地推理网关等）。请求体采用多模态 chat 格式：
一条用户消息内联「一段文本 + 一张 base64 内联图片」。

★ 复果真进化（相对上游 MIT 原仓）
上游在事件循环上**同步**发起 ``chat.completions.create``，而视觉推理是数百毫秒级的
阻塞网络调用，会冻结整个单进程事件循环——所有设备的 WebSocket、语音对讲、事件派发
会同时卡死。本实现把阻塞调用经 ``asyncio.to_thread`` 丢到线程池执行，事件循环在推理
期间持续响应。这是相对上游的唯一结构性改动；其余 API 形态受 OpenAI SDK 契约锁定，
无法在不破坏对接能力的前提下更改。
"""

import asyncio
import openai
from fuguoai_config.logger import setup_logging
from fuguoai_core.utils.util import check_model_key
from fuguoai_core.providers.vllm.base import VLLMProviderBase

_LOG = setup_logging()

# 中文回复约束：在用户提问后追加，要求模型以中文作答。
_CN_SUFFIX = "(请使用中文回复)"
# 内联图片的数据 URI 前缀（OpenAI 多模态协议固定形态）。
_DATA_URI_PREFIX = "data:image/jpeg;base64,"

# 采样参数契约：键名必须与配置 schema 对齐；缺省或非法值一律回落默认。
_SAMPLING = {
    "max_tokens": (500, int),
    "temperature": (0.7, lambda x: round(float(x), 1)),
    "top_p": (1.0, lambda x: round(float(x), 1)),
}


class VLLMProvider(VLLMProviderBase):
    """OpenAI 兼容视觉推理后端。"""

    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.api_key = config.get("api_key")
        # 两种配置写法都兼容：优先 base_url，回退 url。
        if "base_url" in config:
            self.base_url = config.get("base_url")
        else:
            self.base_url = config.get("url")

        for key, (default, coerce) in _SAMPLING.items():
            raw = config.get(key)
            try:
                value = coerce(raw) if raw not in (None, "") else default
            except (ValueError, TypeError):
                value = default
            setattr(self, key, value)

        warn = check_model_key("VLLM", self.api_key)
        if warn:
            _LOG.bind(tag=__name__).error(warn)

        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)

    @staticmethod
    def _build_payload(question, base64_image):
        """拼出 OpenAI 多模态 chat 消息体（文本 + 内联图片）。"""
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question + _CN_SUFFIX},
                    {
                        "type": "image_url",
                        "image_url": {"url": _DATA_URI_PREFIX + base64_image},
                    },
                ],
            }
        ]

    async def response(self, question, base64_image):
        """以「文本 + 图片」发起一次视觉推理，返回模型回复文本。"""
        payload = self._build_payload(question, base64_image)
        try:
            # 阻塞网络调用经线程池执行，避免冻结事件循环（复果真进化）。
            reply = await asyncio.to_thread(
                self.client.chat.completions.create,
                model=self.model_name,
                messages=payload,
                stream=False,
            )
            return reply.choices[0].message.content
        except Exception as exc:  # noqa: BLE001 - 透传给调用方处理
            _LOG.bind(tag=__name__).error(f"视觉推理失败: {exc}")
            raise
