"""对话历史与上下文窗口管理（复果自有实现）。

`Dialogue` 是每条连接的对话上下文容器：保存 system 提示、few-shot 示例与实际
对话历史，并按需拼装成大模型可直接消费的 `List[Dict]`。

复果自有演进点：

* **动态 system prompt 键控 memo**：动态段（时间、记忆、说话人）每轮重渲代价高
  （多 KB 文本要重跑时间替换 + `<memory>` 正则替换 + 说话人拼接）。实际输入
  （模板 / 记忆 / 当前分钟 / 说话人列表）在会话内绝大多数轮次不变，故按这四元组
  键控缓存命中时直接复用上次渲染结果（与逐次渲染字节级一致）。
* **静态 / 动态 system 分段**：以 `<context>` 为界拆成两条 system 消息 —— 静态段
  保持前缀缓存可命中，动态段仍保持 system 角色以确保模型权威性。
* **悬空 tool_calls 补全**：被打断时 assistant 的 tool_calls 可能没有对应 tool
  响应，直接送模型会 400；此处补占位 tool 消息。
* **上下文窗口滑动 + 长尾压缩**：超预算的老消息压成一行历史摘要（不额外调 LLM），
  不修改已存储的对话。
* **跨线程安全**：`put` 在 executor 线程并发调用、事件循环协程并发读取，用
  `RLock` 串行化。
"""

import uuid
import re
import threading
from typing import List, Dict
from datetime import datetime, timezone, timedelta


class Message:
    def __init__(
            self,
            role: str,
            content: str = None,
            uniq_id: str = None,
            tool_calls=None,
            tool_call_id=None,
            is_temporary=False,
    ):
        self.uniq_id = uniq_id if uniq_id is not None else str(uuid.uuid4())
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.is_temporary = is_temporary  # 标记临时消息（如工具调用提醒）


class Dialogue:
    def __init__(self):
        self.dialogue: List[Message] = []
        # 跨线程/协程安全锁：chat() 在 executor 线程并发 put，
        # 同时事件循环协程可能读取，必须串行化。可重入（update_system_message
        # 持锁后再调 put）。
        self._lock = threading.RLock()
        # 获取当前时间（北京时间 +8）
        self.current_time = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        # 动态 system prompt 渲染 memo。
        # get_llm_dialogue_with_memory 每轮至少调用一次，若每轮都重跑
        # 时间替换 + <memory> 正则替换 + 说话人拼接，开销与文本长度成正比。
        # 实际输入(模板/记忆/分钟/说话人)在会话内绝大多数轮次不变，
        # 键控缓存命中时直接复用上次渲染结果（输出与逐次渲染字节级一致）。
        self._dyn_cache_key = None
        self._dyn_cache_val = None
        self._dyn_render_count = 0  # 诊断计数：实际执行渲染的次数(测试钉命中率)

    def put(self, message: Message):
        with self._lock:
            self.dialogue.append(message)

    def getMessages(self, m, dialogue):
        if m.tool_calls is not None:
            dialogue.append({"role": m.role, "tool_calls": m.tool_calls})
        elif m.role == "tool":
            dialogue.append(
                {
                    "role": m.role,
                    "tool_call_id": (
                        str(uuid.uuid4()) if m.tool_call_id is None else m.tool_call_id
                    ),
                    "content": m.content,
                }
            )
        else:
            dialogue.append({"role": m.role, "content": m.content})

    def get_llm_dialogue(self) -> List[Dict[str, str]]:
        # 直接调用 get_llm_dialogue_with_memory，传入 None 作为 memory_str，
        # 这样确保说话人功能在所有调用路径下都生效。
        return self.get_llm_dialogue_with_memory(None, None)

    def update_system_message(self, new_content: str):
        """更新或添加系统消息"""
        with self._lock:
            # 查找第一个系统消息
            system_msg = next((msg for msg in self.dialogue if msg.role == "system"), None)
            if system_msg:
                system_msg.content = new_content
            else:
                self.put(Message(role="system", content=new_content))

    def _ensure_tool_calls_complete(self, messages: List[Message]) -> List[Message]:
        """确保所有 tool_calls 都有对应的 tool 响应。

        修复被打断导致的悬空 tool_calls，防止大模型 API 报 400 错误。
        """
        pending_tool_calls = set()
        result = []

        for msg in messages:
            result.append(msg)

            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        pending_tool_calls.add(tc_id)

            elif msg.role == "tool" and msg.tool_call_id:
                pending_tool_calls.discard(msg.tool_call_id)

        for missing_id in pending_tool_calls:
            dummy_tool_msg = Message(
                role="tool",
                content='{"status": "interrupted", "message": "动作已取消/被打断"}',
                tool_call_id=missing_id
            )
            result.append(dummy_tool_msg)

        return result

    def _render_dynamic_part(self, dynamic_template, memory_str, voiceprint_config):
        """渲染动态上下文 system prompt，带键控 memo。

        键 = (模板, 记忆, 当前分钟, 说话人列表)。会话内绝大多数轮次四者均不变，
        命中时直接返回上次渲染结果；任一变化(跨分钟/记忆更新/模板重建)自动失效。
        """
        now_minute = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
        try:
            _speakers_key = tuple(voiceprint_config.get("speakers", []) or ())
        except Exception:
            _speakers_key = ()
        key = (dynamic_template, memory_str, now_minute, _speakers_key)
        if key == self._dyn_cache_key:
            return self._dyn_cache_val

        dynamic_part = dynamic_template
        # 替换时间占位符
        dynamic_part = dynamic_part.replace("{{current_time}}", now_minute)

        # 填充记忆
        if memory_str is not None:
            dynamic_part = re.sub(
                r"<memory>.*?</memory>",
                f"<memory>\n{memory_str}\n</memory>",
                dynamic_part,
                flags=re.DOTALL,
            )

        # 追加说话人信息
        try:
            speakers = voiceprint_config.get("speakers", [])
            if speakers:
                dynamic_part += "\n<speakers_info>"
                for speaker_str in speakers:
                    try:
                        parts = speaker_str.split(",", 2)
                        if len(parts) >= 2:
                            name = parts[1].strip()
                            description = (
                                parts[2].strip() if len(parts) >= 3 else ""
                            )
                            dynamic_part += f"\n- {name}：{description}"
                    except Exception:
                        pass
                dynamic_part += "\n</speakers_info>"
        except Exception:
            pass

        self._dyn_cache_key = key
        self._dyn_cache_val = dynamic_part
        self._dyn_render_count += 1
        return dynamic_part

    def get_llm_dialogue_with_memory(
            self, memory_str: str = None, voiceprint_config: dict = None,
            window_conf: dict = None,
    ) -> List[Dict[str, str]]:
        # 构建对话
        dialogue = []

        # 添加系统提示和记忆
        with self._lock:
            system_message = next(
                (msg for msg in self.dialogue if msg.role == "system"), None
            )

        if system_message:
            # 以 <context> 为分界点，拆分静态 system prompt 和动态上下文
            # 静态部分（规则、身份等）保持不变，可命中前缀缓存
            # 动态部分（时间、天气、记忆等）作为第二条 system 消息，保持 system 权威性
            full_prompt = system_message.content
            context_match = re.search(r"<context>", full_prompt)
            if context_match:
                static_part = full_prompt[:context_match.start()]
                dynamic_part = full_prompt[context_match.start():]
            else:
                static_part = full_prompt
                dynamic_part = ""

            # 第一段：静态 system prompt（前缀缓存可命中）
            dialogue.append({"role": "system", "content": static_part})

        # 第二段：few-shot 示例（会话内不变，也是缓存前缀的一部分）
        with self._lock:
            non_system_messages = [m for m in self.dialogue if m.role != "system"]
        fewshot_messages = [m for m in non_system_messages if m.is_temporary]
        complete_fewshot = self._ensure_tool_calls_complete(fewshot_messages)
        for m in complete_fewshot:
            self.getMessages(m, dialogue)

        # 第三段：动态上下文 system prompt（时间、记忆、说话人等）
        # 保持 system 角色以确保模型权威性，不降级为 user
        if system_message and dynamic_part:
            dialogue.append(
                {
                    "role": "system",
                    "content": self._render_dynamic_part(
                        dynamic_part, memory_str, voiceprint_config
                    ),
                }
            )

        # 第四段：实际对话历史（不含 few-shot）
        actual_messages = [m for m in non_system_messages if not m.is_temporary]
        # 上下文窗口管理（滑动窗口 + 长尾压缩），不修改已存储的对话
        if window_conf:
            actual_messages = self._apply_window(actual_messages, window_conf)
        complete_actual = self._ensure_tool_calls_complete(actual_messages)
        for m in complete_actual:
            self.getMessages(m, dialogue)

        return dialogue

    # ------------------------------------------------------------------
    # 上下文窗口管理
    # ------------------------------------------------------------------
    @staticmethod
    def _turn_chars(msgs):
        return sum(len(m.content or "") for m in msgs if m.role in ("user", "assistant"))

    def _rollup(self, msgs):
        """把被驱逐的老消息压缩为一行历史摘要（轻量，无额外 LLM 调用）。"""
        parts = []
        for m in msgs:
            c = (m.content or "").strip()
            if not c:
                continue
            c = c[:60] + ("…" if len(c) > 60 else "")
            parts.append(f"{'用户' if m.role == 'user' else '助手'}: {c}")
        return "；".join(parts)

    def _apply_window(self, messages, conf):
        max_rounds = int(conf.get("max_rounds", 20))
        max_chars = int(conf.get("max_history_chars", 6000))
        if len(messages) <= max_rounds * 2 and self._turn_chars(messages) <= max_chars:
            return messages
        keep = max(2, max_rounds * 2)
        evicted = messages[:-keep] if len(messages) > keep else []
        recent = list(messages[-keep:]) if len(messages) > keep else list(messages)
        # 仍超字符预算则继续从 recent 头部压缩，至少保留最近 2 条
        while self._turn_chars(recent) > max_chars and len(recent) > 2:
            evicted.append(recent.pop(0))
        if not evicted:
            return messages  # 预算内，原样返回
        summary = self._rollup(evicted)
        if summary:
            try:
                from fuguoai_core.utils.metrics import METRICS
                if METRICS:
                    METRICS.inc("fuguoai_context_window_trims_total")
            except Exception:
                pass
            return [
                Message(role="system", content=f"<history_summary>{summary}</history_summary>")
            ] + recent
        return recent
