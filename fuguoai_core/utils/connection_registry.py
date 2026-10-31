"""设备在线状态注册表（轻量内存态，旁路非侵入）。

只记录「当前实时在线」的设备，不入对话路径、不持久化、不影响 TTS / 断句 / LLM。
建连时 `mark_online`、断开时 `mark_offline`；设备面板端点读取快照。
单机多连接场景用 session_id 区分，避免串扰。

**连接实例身份 conn_uid**
固件对「主对话 WS」与「intercom 音频 WS」**复用同一个 session_id**（实测两条连接
session_id 完全相同），使原先仅凭 session_id 的防串扰判据彻底失效 —— 旧连接的迟到
断开（TCP 死链可滞后数十秒）会以「session_id 相同」为由注销掉活跃新连接的
`_online` / `_send` / `_kick` 登记，连锁触发 intercom 取不到连接 → 下行
peer_offline、看门狗判「设备假在线」下发 stop → 会话被拆 → 设备后续上行帧全部落
`not_registered`。故所有登记项额外携带 `conn_uid`（每条 WS 连接唯一，由建连方生成），
注销 / 接管判定**优先按 conn_uid 精确匹配**；调用方未提供 conn_uid 时退化为原
session_id 行为，保证旧调用方与既有测试不受影响。

**事件旁路**
在线 / 下线 / 活动三类事件经事件总线旁路发布，发布失败绝不影响在线态登记本身。

**线程安全**：所有读写走同一把锁。
"""

import json
import threading
import time

TAG = __name__

# 设备活动事件的最小发布间隔（秒），避免高频 touch 刷屏订阅者。
_ACTIVE_PUBLISH_INTERVAL = 3.0


def _publish(event: dict):
    """旁路发布实时事件；失败绝不影响在线态登记本身。"""
    try:
        from fuguoai_core.utils.event_bus import get_event_bus
        get_event_bus().publish(event)
    except Exception:
        pass


def _rec_owned_by(rec, session_id: str, conn_uid: str = None) -> bool:
    """判定登记项 rec 是否确实属于 (session_id, conn_uid) 这条连接。

    双方都带 conn_uid 时以 conn_uid 为准 —— 精确到「连接实例」，这是修复固件复用
    session_id 后新旧连接不可分的关键；任一侧缺失 conn_uid 时退化为 session_id
    比对，保持旧调用方（未传 conn_uid）与既有测试的行为不变。
    """
    if not rec:
        return False
    rec_uid = rec.get("conn_uid")
    if conn_uid is not None and rec_uid is not None:
        return rec_uid == conn_uid
    return rec.get("session_id") == session_id


def _same_conn(rec_a, rec_b) -> bool:
    """判定两条登记项（_online 与 _send/_kick）是否指向同一条连接。

    两侧都带 conn_uid 时按 conn_uid，否则退化 session_id（向后兼容）。
    """
    if not rec_a or not rec_b:
        return False
    uid_a, uid_b = rec_a.get("conn_uid"), rec_b.get("conn_uid")
    if uid_a is not None and uid_b is not None:
        return uid_a == uid_b
    return rec_a.get("session_id") == rec_b.get("session_id")


class ConnectionRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        # device_id -> {session_id, conn_uid, connected_at, last_active, client_ip}
        self._online = {}
        # 每设备上次发布 active 事件的时间戳（节流用）
        self._last_active_pub = {}
        # 踢设备：device_id -> {session_id, conn_uid, factory}；factory() 返回需 await 的关闭协程
        self._kick = {}
        # 向设备 WS 下发消息（如 device.bound）：device_id -> {session_id, conn_uid, factory}；
        # factory() 返回 connection 对象，send_to_device 经 connection.safe_send 串行下发。
        self._send = {}

    def mark_online(self, device_id: str, session_id: str, client_ip: str = None,
                    conn_uid: str = None):
        if not device_id:
            return
        now = time.time()
        with self._lock:
            self._online[device_id] = {
                "session_id": session_id,
                "conn_uid": conn_uid,
                "connected_at": now,
                "last_active": now,
                "client_ip": client_ip,
            }
        # 旁路：设备上线事件
        _publish({
            "type": "device.online",
            "device_id": device_id,
            "session_id": session_id,
            "connected_at": int(now),
            "client_ip": client_ip,
            "ts": int(now),
        })

    def mark_offline(self, device_id: str, session_id: str, conn_uid: str = None):
        if not device_id:
            return
        removed = False
        with self._lock:
            rec = self._online.get(device_id)
            # 仅当确属本连接才清除：优先 conn_uid 精确匹配（固件复用 session_id 时，
            # 旧连接迟到断开绝不能抹掉活跃新连接的在线态），退化时按 session_id。
            if _rec_owned_by(rec, session_id, conn_uid):
                self._online.pop(device_id, None)
                self._last_active_pub.pop(device_id, None)
                removed = True
        # 旁路：仅在确实下线时发事件
        if removed:
            _publish({
                "type": "device.offline",
                "device_id": device_id,
                "ts": int(time.time()),
            })

    def touch(self, device_id: str, session_id: str = None, conn_uid: str = None):
        """更新 last_active（设备有活动时调用，例如收到文本）。"""
        if not device_id:
            return
        now = time.time()
        publish_active = False
        with self._lock:
            rec = self._online.get(device_id)
            if rec and (session_id is None or _rec_owned_by(rec, session_id, conn_uid)):
                rec["last_active"] = now
                # 节流：距上次发布超过间隔才再发，避免刷屏
                last_pub = self._last_active_pub.get(device_id, 0)
                if now - last_pub >= _ACTIVE_PUBLISH_INTERVAL:
                    self._last_active_pub[device_id] = now
                    publish_active = True
        # 旁路：设备活动事件（节流后）
        if publish_active:
            _publish({
                "type": "device.active",
                "device_id": device_id,
                "last_active": int(now),
                "ts": int(now),
            })

    def is_online(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self._online

    def snapshot(self, device_id: str):
        with self._lock:
            rec = self._online.get(device_id)
            return dict(rec) if rec else None

    def get_online_device_ids(self):
        with self._lock:
            return list(self._online.keys())

    def is_taken_over(self, device_id: str, session_id: str, conn_uid: str = None) -> bool:
        """本连接是否已被同设备的更新连接接管（当前在线态不属于本连接）。

        供建连层的保护使用：设备旧连接 TCP 死链可滞后数十秒才被读循环发现，其迟到
        断开绝不能把已由新连接接管的活跃对讲会话打入 grace。原实现直接比对 session_id，
        在固件复用 session_id 时判不出接管（恒为 False）；改走 conn_uid 后可精确识别。

        设备完全离线（无在线记录）时返回 False —— 此时确应走正常的 grace 兜底。
        """
        if not device_id:
            return False
        with self._lock:
            rec = self._online.get(device_id)
            if not rec:
                return False
            return not _rec_owned_by(rec, session_id, conn_uid)

    # ====================== 踢设备（解绑/下线时主动断开 WS） ======================
    def register_kick(self, device_id: str, session_id: str, factory,
                      conn_uid: str = None):
        """注册该设备活跃连接的关闭协程工厂（factory() 返回需 await 的协程）。

        在建连层 mark_online 时调用，factory 用惰性 lambda 读取 websocket，
        避免连接建立早期 websocket 尚未赋值导致的时序问题。
        """
        if not device_id:
            return
        with self._lock:
            self._kick[device_id] = {"session_id": session_id,
                                     "conn_uid": conn_uid, "factory": factory}

    def unregister_kick(self, device_id: str, session_id: str, conn_uid: str = None):
        """连接正常关闭时注销踢回调（防 session/连接实例串扰）。"""
        if not device_id:
            return
        with self._lock:
            rec = self._kick.get(device_id)
            if _rec_owned_by(rec, session_id, conn_uid):
                self._kick.pop(device_id, None)

    # ====================== 向设备 WS 下发消息（如 device.bound） ======================
    def register_send(self, device_id: str, session_id: str, factory,
                      conn_uid: str = None):
        """注册该设备活跃连接的发送目标工厂（factory() 返回 connection 对象）。

        在建连层 mark_online 时调用，factory 用惰性 lambda 读取连接对象自身，
        避免连接建立早期 websocket 尚未赋值导致的时序问题（与 register_kick 一致）。
        """
        if not device_id:
            return
        with self._lock:
            self._send[device_id] = {"session_id": session_id,
                                     "conn_uid": conn_uid, "factory": factory}

    def unregister_send(self, device_id: str, session_id: str, conn_uid: str = None):
        """连接正常关闭时注销发送目标（防 session/连接实例串扰）。

        对讲关键路径：设备旧连接迟到断开时若误注销新连接的 _send，取连接立即返回
        None，手机→设备下行整段变 peer_offline。
        """
        if not device_id:
            return
        with self._lock:
            rec = self._send.get(device_id)
            if _rec_owned_by(rec, session_id, conn_uid):
                self._send.pop(device_id, None)

    async def send_to_device(self, device_id: str, message) -> bool:
        """向设备活跃 WS 下发一条消息（如 device.bound 清 need_bind）。

        设备不在线 / session 不匹配 / 发送异常时返回 False（绝不抛异常影响调用方）。
        message 可为 str 或 dict；dict 会被 JSON 序列化。
        """
        if not device_id:
            return False
        with self._lock:
            send_rec = self._send.get(device_id)
            if not send_rec:
                return False
            online_rec = self._online.get(device_id)
            if not _same_conn(online_rec, send_rec):
                self._send.pop(device_id, None)
                return False
            factory = send_rec["factory"]
        try:
            conn = factory()
            if conn is None:
                return False
            if hasattr(conn, "safe_send"):
                await conn.safe_send(message if isinstance(message, str) else json.dumps(message, ensure_ascii=False))
            elif getattr(conn, "websocket", None) is not None:
                await conn.websocket.send_str(message if isinstance(message, str) else json.dumps(message, ensure_ascii=False))
            else:
                return False
            return True
        except Exception:
            return False

    def kick(self, device_id: str):
        """取出该设备当前活跃连接的关闭协程工厂列表（调用后即从登记移除）。

        仅当设备确实在线且 session 匹配时才返回，否则返回空列表。
        返回的 factory 需在事件循环中 await。
        """
        if not device_id:
            return []
        with self._lock:
            kick_rec = self._kick.get(device_id)
            if not kick_rec:
                return []
            online_rec = self._online.get(device_id)
            if not _same_conn(online_rec, kick_rec):
                self._kick.pop(device_id, None)
                return []
            factory = kick_rec["factory"]
            self._kick.pop(device_id, None)
            return [factory]


_REGISTRY = None


def get_connection_registry() -> ConnectionRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ConnectionRegistry()
    return _REGISTRY
