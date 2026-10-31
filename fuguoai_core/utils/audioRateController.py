"""复果音频速率控制器：把 opus 包的下发节奏钉在虚拟播放时钟上。

设备侧按固定帧长（默认 60ms）消费音频。若每发一包就 sleep 一个帧长，
sleep 自身的调度误差会逐包累积，长时间播放后音画/播报节奏整体漂移。
本控制器改为维护一条「虚拟播放位置」（play_position），每发一包推进一个帧长，
下发前只补齐「真实时间」与「虚拟位置」之间的差额，从而消除累积误差。

队列内两类条目：
- audio：占用播放时长，必须等虚拟播放位置追上真实时间才下发；
- message：控制指令（如句首 sentence_start），不占播放时间，轮到即刻下发，
  用于工具调用等待期仍要把控制面消息送出去。

本模块位于实时下发关键路径：本轮重编只改内部组织（条目表示 / 分派结构 /
事件状态维护 / 等待循环表达），计时语义（何时 sleep、sleep 多久、play_position
如何推进、条目何时出队）与原实现逐点等价——见 cleanroom/audioRateController_spec.md。
"""

import asyncio
import time
from collections import deque

from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 条目种类：决定下发语义（是否占用播放时间）
_KIND_AUDIO = "audio"
_KIND_MESSAGE = "message"


class _PendingItem:
    """待下发条目。kind 决定语义，payload 为 opus 包或消息回调。"""

    __slots__ = ("kind", "payload")

    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload


class AudioRateController:
    """按帧时长精确控制音频下发的速率控制器。"""

    def __init__(self, frame_duration: int = 60):
        self.frame_duration = frame_duration
        self.queue = deque()
        self.play_position = 0
        self.start_timestamp = None
        self.pending_send_task = None
        self.logger = logger
        self.queue_empty_event = asyncio.Event()
        self.queue_empty_event.set()
        self.queue_has_data_event = asyncio.Event()
        self._last_queue_empty_time = 0

    # ------------------------------------------------------------------
    # 内部：状态维护（原实现在入队/排空/重置处重复操作事件，此处收口）
    # ------------------------------------------------------------------
    def _mark_pending(self) -> None:
        """队列由空转非空：置位数据事件、清空空队列事件。"""
        self.queue_empty_event.clear()
        self.queue_has_data_event.set()

    def _mark_drained(self) -> None:
        """队列排空：置位空队列事件、清空数据事件，并记下排空时刻。"""
        self.queue_empty_event.set()
        self.queue_has_data_event.clear()
        self._last_queue_empty_time = time.monotonic()

    def _cancel_task(self) -> None:
        if self.pending_send_task and not self.pending_send_task.done():
            self.pending_send_task.cancel()

    def _take(self):
        return self.queue.popleft()

    # ------------------------------------------------------------------
    # 内部：时钟
    # ------------------------------------------------------------------
    def _elapsed_ms(self) -> float:
        """自基准时间戳起已流逝的毫秒；未起表时为 0。"""
        if self.start_timestamp is None:
            return 0
        return (time.monotonic() - self.start_timestamp) * 1000

    def _rebase_clock_if_resumed(self) -> None:
        """队列由空转非空且间隔超过 1 帧时重定基准时间戳。

        工具调用等待期队列会排空，若不重定基准，新音频会按旧基准立刻到期而「提前播放」；
        间隔不足 1 帧属正常流式续传，不重定。
        """
        if self.play_position <= 0:
            return
        elapsed_since_empty = (time.monotonic() - self._last_queue_empty_time) * 1000
        if elapsed_since_empty < self.frame_duration:
            return
        self.start_timestamp = time.monotonic() - (self.play_position / 1000)
        self.logger.bind(tag=TAG).debug(
            f"队列从空恢复，重置时间戳，当前播放位置: {self.play_position}ms，间隔: {elapsed_since_empty:.0f}ms"
        )

    async def _await_playhead(self) -> None:
        """阻塞到虚拟播放位置追上真实时间；等待可被取消。"""
        while True:
            gap_ms = self.play_position - self._elapsed_ms()
            if gap_ms <= 0:
                return
            try:
                await asyncio.sleep(gap_ms / 1000)
            except asyncio.CancelledError:
                self.logger.bind(tag=TAG).debug("音频发送任务被取消")
                raise

    # ------------------------------------------------------------------
    # 内部：两类条目的下发
    # ------------------------------------------------------------------
    async def _emit_message(self, callback) -> None:
        try:
            await callback()
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"发送消息失败: {e}")
            raise

    async def _emit_audio(self, opus_packet, send_audio_callback) -> None:
        """音频下发：起表 → 等播放位置 → 出队 → 推进 → 发送。

        出队发生在等待**之后**：等待期间该包仍在 queue 内，外部按 len(queue)
        估算堆积时长（tts/base.py）才能算到它。
        """
        if self.start_timestamp is None:
            self.start_timestamp = time.monotonic()
        await self._await_playhead()
        self._take()
        self.play_position += self.frame_duration
        try:
            await send_audio_callback(opus_packet)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"发送音频失败: {e}")
            raise

    def _enqueue(self, kind, payload) -> None:
        if not self.queue:
            self._rebase_clock_if_resumed()
        self.queue.append(_PendingItem(kind, payload))
        self._mark_pending()

    # ------------------------------------------------------------------
    # 公共 API（活契约）
    # ------------------------------------------------------------------
    def add_audio(self, opus_packet) -> None:
        """把 opus 包排入下发队列（占用播放时长）。"""
        self._enqueue(_KIND_AUDIO, opus_packet)

    def add_message(self, message_callback) -> None:
        """把控制消息排入队列（不占播放时间，轮到即刻下发）。

        Args:
            message_callback: 消息发送回调 async def()
        """
        self._enqueue(_KIND_MESSAGE, message_callback)

    async def check_queue(self, send_audio_callback) -> None:
        """按虚拟播放时钟下发队列内条目，直到排空。

        Args:
            send_audio_callback: 音频发送回调 async def(opus_packet)
        """
        while self.queue:
            item = self.queue[0]
            if item.kind == _KIND_MESSAGE:
                # 消息不占播放时间：先出队再回调
                self._take()
                await self._emit_message(item.payload)
            elif item.kind == _KIND_AUDIO:
                # 音频须等播放位置：出队时机在 _emit_audio 内部（等待之后）
                await self._emit_audio(item.payload, send_audio_callback)
        self._mark_drained()

    def start_sending(self, send_audio_callback) -> asyncio.Task:
        """启动后台下发循环，返回其 Task。"""

        async def _pump():
            try:
                while True:
                    await self.queue_has_data_event.wait()
                    await self.check_queue(send_audio_callback)
            except asyncio.CancelledError:
                self.logger.bind(tag=TAG).debug("音频发送循环已停止")
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"音频发送循环异常: {e}")

        self.pending_send_task = asyncio.create_task(_pump())
        return self.pending_send_task

    def stop_sending(self) -> None:
        """取消后台下发任务。"""
        if self.pending_send_task and not self.pending_send_task.done():
            self.pending_send_task.cancel()
            self.logger.bind(tag=TAG).debug("已取消音频发送任务")

    def reset(self) -> None:
        """重置控制器状态（取消未决任务、清空队列与事件）。"""
        self._cancel_task()
        self.queue.clear()
        self.play_position = 0
        self.start_timestamp = None
        self._last_queue_empty_time = 0
        self.queue_empty_event.set()
        self.queue_has_data_event.clear()
