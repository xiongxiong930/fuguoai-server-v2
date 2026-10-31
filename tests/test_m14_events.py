"""M14 事件推送 · 核心逻辑单元测试（无服务器依赖，可本地/容器直接跑）。

覆盖：
  A. event_bus 无订阅者时 publish 为 no-op（零开销、零风险）
  B. 跨线程 publish → 订阅者 loop 内收到事件
  C. 慢消费者：队列满丢最旧，发布方不阻塞
  D. connection_registry 旁路发布：mark_online/touch/mark_offline 触发对应事件
  E. touch 节流：短间隔内不重复发 device.active

运行：
  cd <项目根> && python3 test_m14_events.py
"""
import asyncio
import sys
import threading
import time

from fuguoai_core.utils.event_bus import EventBus, get_event_bus, _QUEUE_MAXSIZE
from fuguoai_core.utils.connection_registry import ConnectionRegistry, _ACTIVE_PUBLISH_INTERVAL

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")


# ── A. 无订阅者 no-op ────────────────────────────────────────
def test_a_noop():
    bus = EventBus()
    check("A1 无订阅者 has_subscribers=False", bus.has_subscribers() is False)
    # 不应抛异常、不应阻塞
    bus.publish({"type": "x", "device_id": "d"})
    check("A2 无订阅者 publish 静默返回", True)


# ── B. 跨线程 publish → 订阅者收到 ───────────────────────────
async def test_b_cross_thread():
    bus = EventBus()
    sid, queue = bus.subscribe()
    check("B1 订阅后 has_subscribers=True", bus.has_subscribers() is True)

    # 在另一个线程发布（模拟设备同步线程）
    def worker():
        time.sleep(0.05)
        bus.publish({"type": "device.online", "device_id": "dev-A"})

    threading.Thread(target=worker, daemon=True).start()
    event = await asyncio.wait_for(queue.get(), timeout=2.0)
    check("B2 跨线程事件被收到", event.get("device_id") == "dev-A")
    check("B3 事件自动补 ts", isinstance(event.get("ts"), int))

    bus.unsubscribe(sid)
    check("B4 取消订阅后 has_subscribers=False", bus.has_subscribers() is False)


# ── C. 慢消费者丢最旧 ────────────────────────────────────────
def test_c_slow_consumer():
    bus = EventBus()

    async def _run():
        sid, queue = bus.subscribe()
        # 直接同步灌入 maxsize+50 个，触发丢最旧
        overflow = _QUEUE_MAXSIZE + 50
        for i in range(overflow):
            EventBus._deliver(queue, {"seq": i})
        check("C1 队列不超过上限", queue.qsize() == _QUEUE_MAXSIZE)
        first = queue.get_nowait()
        # 丢掉了最旧的 50 个，队首应为 seq=50
        check("C2 丢弃最旧、保留最新", first.get("seq") == 50)
        bus.unsubscribe(sid)

    asyncio.run(_run())


# ── D & E. connection_registry 旁路发布 + 节流 ───────────────
async def test_d_registry_bypass():
    bus = get_event_bus()  # connection_registry._publish 用的是全局单例
    sid, queue = bus.subscribe()
    reg = ConnectionRegistry()

    reg.mark_online("dev-B", "sess-1", client_ip="1.2.3.4")
    e1 = await asyncio.wait_for(queue.get(), timeout=2.0)
    check("D1 mark_online → device.online",
          e1.get("type") == "device.online" and e1.get("device_id") == "dev-B")
    check("D2 online 事件带 client_ip", e1.get("client_ip") == "1.2.3.4")

    reg.touch("dev-B", "sess-1")
    e2 = await asyncio.wait_for(queue.get(), timeout=2.0)
    check("D3 首次 touch → device.active", e2.get("type") == "device.active")

    # E. 节流：紧接着再 touch 不应再发（间隔 < _ACTIVE_PUBLISH_INTERVAL）
    reg.touch("dev-B", "sess-1")
    try:
        extra = await asyncio.wait_for(queue.get(), timeout=0.5)
        # 若拿到的是 offline 说明顺序异常；这里期望超时
        throttled = extra.get("type") != "device.active"
    except asyncio.TimeoutError:
        throttled = True
    check("E1 短间隔重复 touch 被节流", throttled)

    reg.mark_offline("dev-B", "sess-1")
    e3 = await asyncio.wait_for(queue.get(), timeout=2.0)
    check("D4 mark_offline → device.offline",
          e3.get("type") == "device.offline" and e3.get("device_id") == "dev-B")

    # 不匹配 session 的 offline 不应清除、不应发事件
    reg.mark_online("dev-C", "sess-2")
    _ = await asyncio.wait_for(queue.get(), timeout=2.0)  # 消费 online
    reg.mark_offline("dev-C", "WRONG-SESS")
    try:
        await asyncio.wait_for(queue.get(), timeout=0.5)
        wrong_ok = False
    except asyncio.TimeoutError:
        wrong_ok = True
    check("D5 session 不匹配的 offline 不发事件", wrong_ok)

    bus.unsubscribe(sid)


def main():
    test_a_noop()
    asyncio.run(test_b_cross_thread())
    test_c_slow_consumer()
    asyncio.run(test_d_registry_bypass())

    print("\n==== M14 汇总 ====")
    print(f"PASS={len(PASS)}  FAIL={len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
