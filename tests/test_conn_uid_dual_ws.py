"""离线回归：对讲双 WS 复用 session_id 的注册防串扰（conn_uid，2026-08-02）。

复现线上根因：固件对「主对话 WS」与「对讲音频 WS」复用同一 session_id
（实测 94a9901a8818 两条连接均为 SiTeOpTeshcaCh）。旧实现仅凭 session_id 做
防串扰判据，旧连接的 TCP 死链可滞后数十秒才断开，其迟到 mark_offline /
unregister_send 会以「session_id 相同」为由误删活跃新连接的 _online/_send 登记，
连锁触发 intercom _get_conn()=None → 下行 peer_offline、watchdog 判「设备假在线」
下发 SetIntercom stop → 会话被拆 → 设备后续上行帧全落 not_registered（实测 10030 次）。

修复：所有登记项额外携带 conn_uid（每条 WS 连接唯一，由 connection.py 生成），
注销/接管判定优先按 conn_uid 精确匹配；调用方未传 conn_uid 时退化为原 session_id
行为，保证旧调用方与既有测试不受影响。

测试不依赖网络/容器，直接驱动 ConnectionRegistry，并用 intercom_registry._get_conn
做端到端一致性校验（覆盖 relay 下行与上行登记可读的关键路径）。
"""

import importlib

import fuguoai_core.utils.connection_registry as cr_mod
from fuguoai_core.utils.connection_registry import ConnectionRegistry

# 固定 device/session，复现 cap_v3.log / live grep 159 实测三元组
D = "dev94a9901a8818"
S = "SiTeOpTeshcaCh"


def _fresh_registry() -> ConnectionRegistry:
    return ConnectionRegistry()


def _patch_global_registry(reg: ConnectionRegistry):
    """把 connection_registry 模块单例替换为测试实例（_get_conn 经它取连接）。"""
    saved = cr_mod._REGISTRY
    cr_mod._REGISTRY = reg
    return saved


def _restore_global_registry(saved):
    cr_mod._REGISTRY = saved


def test_old_main_ws_late_disconnect_keeps_active_intercom_registration():
    """核心场景：主对话 WS 死链迟到断开，绝不能误删活跃对讲音频 WS 的登记。

    这是 2026-08-02 根因的直接反演——旧实现会误删，导致 relay 拿不到连接。
    """
    reg = _fresh_registry()
    u_main = "a" * 32
    u_inter = "b" * 32

    # 阶段1：主对话 WS 上线
    reg.mark_online(D, S, conn_uid=u_main)
    reg.register_kick(D, S, lambda: None, conn_uid=u_main)
    reg.register_send(D, S, lambda: "MAIN_CONN", conn_uid=u_main)

    # 阶段2：对讲音频 WS 上线（接管 _send/_online 槽位，conn_uid 不同）
    reg.mark_online(D, S, conn_uid=u_inter)
    reg.register_kick(D, S, lambda: None, conn_uid=u_inter)
    reg.register_send(D, S, lambda: "INTERCOM_CONN", conn_uid=u_inter)

    # 阶段3：主 WS 死链迟到断开（F-1 保护：先判定 takeover，再注销自身登记）
    assert reg.is_taken_over(D, S, u_main) is True, "旧主WS应被判定为已被接管"
    reg.mark_offline(D, S, conn_uid=u_main)
    reg.unregister_kick(D, S, conn_uid=u_main)
    reg.unregister_send(D, S, conn_uid=u_main)

    # 阶段4：活跃对讲音频 WS 的登记必须完好，relay 仍能拿到连接
    assert reg.is_online(D), "_online 不应被旧连接误删"
    snap = reg.snapshot(D)
    assert snap["conn_uid"] == u_inter, f"在线态应属于对讲WS: {snap}"
    send_rec = reg._send.get(D)
    assert send_rec is not None and send_rec["conn_uid"] == u_inter, "send 登记应保留对讲WS"

    # 端到端：intercom_registry._get_conn 经 _same_conn 一致性校验应返回对讲连接
    saved = _patch_global_registry(reg)
    try:
        from fuguoai_core.utils.intercom_registry import _get_conn
        conn = _get_conn(D)
        assert conn == "INTERCOM_CONN", f"relay 应拿到对讲音频WS连接, got {conn!r}"
    finally:
        _restore_global_registry(saved)

    print("PASS test_old_main_ws_late_disconnect_keeps_active_intercom_registration")


def test_intercom_ws_late_disconnect_keeps_main_registration():
    """对称场景：对讲音频 WS 死链迟到断开，绝不能误删已回连的主对话 WS 登记。

    复现真实抖动序列：主WS连 → 对讲WS连 → 主WS重连(新uid) → 对讲WS迟到断开。
    """
    reg = _fresh_registry()
    u_main = "m" * 32
    u_inter = "i" * 32
    u_main2 = "n" * 32

    reg.mark_online(D, S, conn_uid=u_main)
    reg.register_send(D, S, lambda: "MAIN", conn_uid=u_main)

    reg.mark_online(D, S, conn_uid=u_inter)
    reg.register_send(D, S, lambda: "INTER", conn_uid=u_inter)

    reg.mark_online(D, S, conn_uid=u_main2)
    reg.register_send(D, S, lambda: "MAIN2", conn_uid=u_main2)

    # 对讲 WS 迟到断开
    assert reg.is_taken_over(D, S, u_inter) is True
    reg.mark_offline(D, S, conn_uid=u_inter)
    reg.unregister_send(D, S, conn_uid=u_inter)

    snap = reg.snapshot(D)
    assert snap["conn_uid"] == u_main2, f"在线态应属于回连的主WS: {snap}"
    assert reg._send.get(D)["conn_uid"] == u_main2, "send 登记应保留主WS新连接"

    print("PASS test_intercom_ws_late_disconnect_keeps_main_registration")


def test_same_conn_uid_disconnect_truly_removes():
    """真离线（同一 conn_uid）断开必须真正移除登记——grace 兜底路径才正确触发。

    conn_uid 修复不能破坏「确属本连接的正常断开」语义，否则 watchdog 永不拆会话。
    """
    reg = _fresh_registry()
    u = "c" * 32
    reg.mark_online(D, S, conn_uid=u)
    reg.register_send(D, S, lambda: "C", conn_uid=u)

    assert reg.is_taken_over(D, S, u) is False, "同 conn_uid 应判定未被接管(真离线)"

    reg.mark_offline(D, S, conn_uid=u)
    assert not reg.is_online(D), "真离线应移除在线态"
    reg.unregister_send(D, S, conn_uid=u)
    assert reg._send.get(D) is None, "真离线应移除 send 登记"

    print("PASS test_same_conn_uid_disconnect_truly_removes")


def test_backward_compat_no_conn_uid_session_collision():
    """调用方未传 conn_uid 时退化为原 session_id 行为，旧调用方/既有测试不受影响。

    无 uid 时两条 WS 仍按 session_id 互相覆盖（最后写者胜），断开按 session_id 命中。
    """
    reg = _fresh_registry()
    reg.mark_online(D, S)                       # 旧主 WS，conn_uid=None
    reg.register_send(D, S, lambda: "OLD")      # conn_uid 默认 None
    # 新连接（同样未传 uid）覆盖槽位——原行为：最后写者胜
    reg.mark_online(D, S)
    reg.register_send(D, S, lambda: "NEW")

    # 旧主 WS 断开（未传 uid）：退化为 session_id 比对 → 命中当前(新)记录并移除
    reg.mark_offline(D, S)
    assert not reg.is_online(D), "无 uid 退化路径应仍按 session_id 移除最后写者"
    reg.unregister_send(D, S)
    assert reg._send.get(D) is None

    # is_taken_over 无 uid 且当前无记录 → False（即便传入一个不存在的 uid）
    assert reg.is_taken_over(D, S) is False

    print("PASS test_backward_compat_no_conn_uid_session_collision")


def test_get_conn_consistency_helper_same_conn():
    """_same_conn / _rec_owned_by 一致性判据：双 uid 按 uid、缺失退化 session_id。"""
    from fuguoai_core.utils.connection_registry import _same_conn, _rec_owned_by

    ra = {"session_id": S, "conn_uid": "x" * 32}
    rb = {"session_id": S, "conn_uid": "x" * 32}
    rc = {"session_id": S, "conn_uid": "y" * 32}

    # 同 uid → 同连接
    assert _same_conn(ra, rb) is True
    # 异 uid → 不同连接（即便 session_id 相同，固件复用场景）
    assert _same_conn(ra, rc) is False

    # _rec_owned_by：双方带 uid 以 uid 为准
    assert _rec_owned_by(ra, S, "x" * 32) is True
    assert _rec_owned_by(ra, S, "y" * 32) is False
    # 任一侧缺失 uid → 退化 session_id
    assert _rec_owned_by({"session_id": S}, S, None) is True
    assert _rec_owned_by({"session_id": S, "conn_uid": "z" * 32}, S, None) is True
    assert _rec_owned_by(None, S, "x" * 32) is False

    print("PASS test_get_conn_consistency_helper_same_conn")


if __name__ == "__main__":
    test_old_main_ws_late_disconnect_keeps_active_intercom_registration()
    test_intercom_ws_late_disconnect_keeps_main_registration()
    test_same_conn_uid_disconnect_truly_removes()
    test_backward_compat_no_conn_uid_session_collision()
    test_get_conn_consistency_helper_same_conn()
    print("ALL PASS")
