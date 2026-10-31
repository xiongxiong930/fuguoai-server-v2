# -*- coding: utf-8 -*-
"""OpenAI 兼容大模型 provider（复果自有实现）。

统一对接 OpenAI / DeepSeek / 通义 / Kimi / 豆包等兼容 `/chat/completions` 的
流式接口。本文件是 LLM 链路的核心实现，承载以下复果自有演进能力：

* **建流弹性**：主通道走熔断器 + 指数退避重试，仅包裹"建流"这一步（尚未产出
  token，安全可重试）；一旦开始流式产出便不再重试，避免半截输出被重复。
* **备用通道降级**：配置了 `fallback_api_key` 时，主通道彻底失败（含熔断开路）
  自动切备用 key/model，不占用主熔断器计数。
* **思考模式自适应禁用**：按 `base_url` 域名对已知会"强制思考"的平台注入
  `extra_body` 关闭思考，避免推理模型在无谓的长链思考上烧 token。
* **用量直写**：流式 `usage` 块首次出现时，把本轮 token 落到设备用量库
  （连接层直传的 device_id 优先，否则回退 contextvar）。
* **思考标签剥离**：兼容部分模型把 `<think>...</think>` 混在正文流里的写法，
  在 yield 前把非激活段裁掉，下游拿到干净文本。

接口契约严格对齐 `fuguoai_core.providers.llm.base.LLMProviderBase`：
`response` 流式 yield 文本分片；`response_with_functions` 流式 yield
`(content, tool_calls)` 二元组。
"""
import httpx
import json
import openai
from openai.types import CompletionUsage
from fuguoai_config.logger import setup_logging
from fuguoai_core.utils.util import check_model_key
from fuguoai_core.providers.llm.base import LLMProviderBase
from fuguoai_core.utils.usage_store import get_usage_store, get_current_device_session
from fuguoai_core.utils.metrics import METRICS
from fuguoai_core.utils.circuit_breaker import (
    get_breaker,
    retry_with_backoff,
    CircuitOpenError,
)
from urllib.parse import urlparse

TAG = __name__
logger = setup_logging()

# 仅对可恢复的瞬时错误退避重试；401/402/403 等确定性认证/配额错误立即失败走降级，
# 避免同名 key 重试白付时间。token 配额类错误（如 402）即由此类拦截。
_RETRYABLE_LLM_ERRORS = tuple(
    e
    for e in (
        getattr(openai, "RateLimitError", None),      # 429 限流
        getattr(openai, "APIConnectionError", None),   # 连接失败
        getattr(openai, "APITimeoutError", None),       # 超时
        getattr(openai, "InternalServerError", None),   # 5xx
    )
    if e is not None
) or (Exception,)

# 需要禁用思考模式的平台域名及其对应参数（默认关闭思考模式）。
THINKING_DISABLED_DOMAINS = {
    "aliyuncs.com": {"enable_thinking": False},
    "bigmodel.cn": {"thinking": {"type": "disabled"}},
    "moonshot.cn": {"thinking": {"type": "disabled"}},
    "volces.com": {"thinking": {"type": "disabled"}},
}


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name")
        self.api_key = config.get("api_key")
        if "base_url" in config:
            self.base_url = config.get("base_url")
        else:
            self.base_url = config.get("url")

        timeout_config = config.get("timeout")
        if isinstance(timeout_config, dict):
            custom_timeout = httpx.Timeout(
                pool=timeout_config.get("pool", 2.0),
                connect=timeout_config.get("connect", 3.0),
                write=timeout_config.get("write", 5.0),
                read=timeout_config.get("read", 60.0),
            )
        elif isinstance(timeout_config, (int, float)) and timeout_config > 0:
            custom_timeout = httpx.Timeout(timeout_config)
        else:
            custom_timeout = httpx.Timeout(300)

        param_defaults = {
            "max_tokens": int,
            "temperature": lambda x: round(float(x), 1),
            "top_p": lambda x: round(float(x), 1),
            "frequency_penalty": lambda x: round(float(x), 1),
        }

        for param, converter in param_defaults.items():
            value = config.get(param)
            try:
                setattr(
                    self,
                    param,
                    converter(value) if value not in (None, "") else None,
                )
            except (ValueError, TypeError):
                setattr(self, param, None)

        logger.debug(
            f"意图识别参数初始化: {self.temperature}, {self.max_tokens}, {self.top_p}, {self.frequency_penalty}"
        )

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)
        self.client = openai.OpenAI(
            api_key=self.api_key, base_url=self.base_url, timeout=custom_timeout
        )

        # 熔断器：连续失败 5 次开路，30s 后半开探测，成功即恢复。
        self._breaker = get_breaker(
            "llm", failure_threshold=5, recovery_timeout=30.0, half_open_max_calls=1
        )
        # 备用通道：配置了 fallback_api_key 时启用。
        self._fallback_client = None
        self._fallback_model = config.get("fallback_model_name") or self.model_name
        fb_key = config.get("fallback_api_key")
        fb_base = config.get("fallback_base_url") or self.base_url
        if fb_key and "你" not in str(fb_key):
            try:
                self._fallback_client = openai.OpenAI(
                    api_key=fb_key, base_url=fb_base, timeout=custom_timeout
                )
                logger.bind(tag=TAG).info(f"LLM 备用通道已启用: model={self._fallback_model}")
            except Exception as e:
                logger.bind(tag=TAG).warning(f"LLM 备用通道初始化失败，忽略: {e}")

    def _create_stream(self, request_params):
        """建立 LLM 流：主通道走熔断器+退避重试；失败/开路时降级到备用通道。

        仅包裹"建流"这一步（尚未产出 token，安全可重试）；一旦开始流式产出便不再重试，
        避免半截输出被重复。返回可迭代的 responses 对象。
        """
        METRICS.inc(
            "fuguoai_llm_requests_total",
            help_text="LLM 建流请求总数",
        )

        def _primary():
            return self.client.chat.completions.create(**request_params)

        try:
            return self._breaker.call(
                lambda: retry_with_backoff(
                    _primary,
                    retries=2,
                    base_delay=0.3,
                    max_delay=2.0,
                    exceptions=_RETRYABLE_LLM_ERRORS,
                    on_retry=lambda n, e: logger.bind(tag=TAG).warning(
                        f"LLM 建流第{n}次重试: {e}"
                    ),
                )
            )
        except Exception as primary_err:
            METRICS.inc(
                "fuguoai_llm_errors_total",
                help_text="LLM 建流失败总数（含开路快速失败）",
            )
            is_open = isinstance(primary_err, CircuitOpenError)
            logger.bind(tag=TAG).error(
                f"LLM 主通道失败{'（熔断开路）' if is_open else ''}: {primary_err}"
            )
            # 降级：切备用通道（不占用主熔断器计数）
            if self._fallback_client is not None:
                METRICS.inc(
                    "fuguoai_llm_fallback_total",
                    help_text="LLM 降级到备用通道总次数",
                )
                logger.bind(tag=TAG).warning("LLM 降级到备用通道重试…")
                fb_params = dict(request_params)
                fb_params["model"] = self._fallback_model
                return self._fallback_client.chat.completions.create(**fb_params)
            raise

    @staticmethod
    def normalize_dialogue(dialogue):
        """自动修复 dialogue 中缺失 content 的消息。"""
        for msg in dialogue:
            if "role" in msg and "content" not in msg:
                msg["content"] = ""
        return dialogue

    def _apply_thinking_disabled(self, request_params: dict):
        """根据域名自动禁用思考模式。"""
        parsed_url = urlparse(self.base_url)
        domain = parsed_url.netloc
        for disabled_domain, params in THINKING_DISABLED_DOMAINS.items():
            if disabled_domain in domain:
                request_params.setdefault("extra_body", {}).update(params)
                logger.bind(tag=TAG).info(f"为域名 {domain} 禁用思考模式，参数: {params}")
                break

    def response(self, session_id, dialogue, device_id=None, device_session_id=None, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)
        _usage_recorded = False  # 本轮 response 仅首次 usage 事件记一次 token 到用量库
        # 用量归属：显式 device_id/device_session_id 优先（连接层直传，跨线程/异步安全，
        # 语音路径走 ASR 工作线程、contextvar 不可见）；否则回退到 contextvar。
        _ds = (device_id, device_session_id) if (device_id and device_session_id) else get_current_device_session()

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
            # 让流式末尾返回 usage 块（DeepSeek/OpenAI 兼容）；否则 Token 消耗日志永不触发。
            "stream_options": {"include_usage": True},
        }

        # 添加可选参数,只有当参数不为None时才添加
        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 禁用思考模式
        self._apply_thinking_disabled(request_params)

        # 发送前载荷拆解（估算，便于定位 token 去向；中英文混合粗略按 chars/2）
        try:
            _msgs = request_params.get("messages") or []
            _msgs_chars = len(json.dumps(_msgs, ensure_ascii=False))
            logger.bind(tag=TAG).info(f"发送载荷｜对话消息 约 {_msgs_chars // 2} tokens（{len(_msgs)} 条）")
        except Exception:
            pass

        responses = self._create_stream(request_params)

        is_active = True
        try:
            for chunk in responses:
                # 优先判定 usage（DeepSeek 会在某个 content 块内携带 usage，不能放到 elif，否则被 choices 分支吞掉）
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    logger.bind(tag=TAG).info(
                        f"Token 消耗：输入 {getattr(usage, 'prompt_tokens', '未知')}，"
                        f"输出 {getattr(usage, 'completion_tokens', '未知')}，"
                        f"共计 {getattr(usage, 'total_tokens', '未知')}"
                    )
                    # 直写用量库：在已标记的设备/会话上下文下，把本轮 token 落到 device_usage
                    # （contextvar 按异步任务隔离；非连接场景如 tourism_kb/family_rag 为 None 则跳过）
                    self.stats.prompt_tokens = int(getattr(usage, 'prompt_tokens', 0) or 0)
                    self.stats.completion_tokens = int(getattr(usage, 'completion_tokens', 0) or 0)
                    if not _usage_recorded:
                            _usage_recorded = True
                            if _ds:
                                try:
                                    get_usage_store().record_llm_tokens(
                                        _ds[0], _ds[1], self.stats.prompt_tokens, self.stats.completion_tokens
                                    )
                                except Exception:
                                    pass
                try:
                    delta = chunk.choices[0].delta if getattr(chunk, "choices", None) else None
                    content = getattr(delta, "content", "") if delta else ""
                except IndexError:
                    content = ""
                if content:
                    if "<think>" in content:
                        is_active = False
                        content = content.split("<think>")[0]
                    if "</think>" in content:
                        is_active = True
                        content = content.split("</think>")[-1]
                    if is_active:
                        yield content
        finally:
            responses.close()

    def response_with_functions(self, session_id, dialogue, functions=None, device_id=None, device_session_id=None, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)
        _usage_recorded = False  # 本轮 response 仅首次 usage 事件记一次 token 到用量库
        # 用量归属：显式 device_id/device_session_id 优先（连接层直传，跨线程/异步安全，
        # 语音路径走 ASR 工作线程、contextvar 不可见）；否则回退到 contextvar。
        _ds = (device_id, device_session_id) if (device_id and device_session_id) else get_current_device_session()

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
            "tools": functions,
            # 让流式末尾返回 usage 块（DeepSeek/OpenAI 兼容）；否则下方 Token 消耗日志永不触发。
            "stream_options": {"include_usage": True},
        }

        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 禁用思考模式
        self._apply_thinking_disabled(request_params)

        # 发送前载荷拆解（估算，便于定位 token 去向；中英文混合粗略按 chars/2）
        try:
            _tools = request_params.get("tools") or []
            _msgs = request_params.get("messages") or []
            _tools_chars = len(json.dumps(_tools, ensure_ascii=False))
            _msgs_chars = len(json.dumps(_msgs, ensure_ascii=False))
            logger.bind(tag=TAG).info(
                f"发送载荷｜工具 {len(_tools)} 个 约 {_tools_chars // 2} tokens｜"
                f"对话消息 约 {_msgs_chars // 2} tokens（{len(_msgs)} 条）"
            )
        except Exception:
            pass

        stream = self._create_stream(request_params)

        try:
            for chunk in stream:
                # 优先判定 usage（DeepSeek 会在某个 content 块内携带 usage，
                # 该块同时含非空的 choices，若放到 elif 会被 choices 分支吞掉，日志永不触发）
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    logger.bind(tag=TAG).info(
                        f"Token 消耗：输入 {getattr(usage, 'prompt_tokens', '未知')}，"
                        f"输出 {getattr(usage, 'completion_tokens', '未知')}，"
                        f"共计 {getattr(usage, 'total_tokens', '未知')}"
                    )
                    # 直写用量库：在已标记的设备/会话上下文下，把本轮 token 落到 device_usage
                    # （contextvar 按异步任务隔离；非连接场景如 tourism_kb/family_rag 为 None 则跳过）
                    self.stats.prompt_tokens = int(getattr(usage, 'prompt_tokens', 0) or 0)
                    self.stats.completion_tokens = int(getattr(usage, 'completion_tokens', 0) or 0)
                    if not _usage_recorded:
                            _usage_recorded = True
                            if _ds:
                                try:
                                    get_usage_store().record_llm_tokens(
                                        _ds[0], _ds[1], self.stats.prompt_tokens, self.stats.completion_tokens
                                    )
                                except Exception:
                                    pass
                if getattr(chunk, "choices", None):
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", "")
                    tool_calls = getattr(delta, "tool_calls", None)
                    yield content, tool_calls
        finally:
            stream.close()
