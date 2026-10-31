# events WS 鉴权对接说明（M14 端点 `/api/v1/events`）

> 状态：已定位根因 + 已修复（后端 `fuguoai_core/utils/user_auth.py` 增加 `?token=` 兜底；前端 `services/events.js` 改发 `?token=`）。
> 适用：小程序 / web 管理面板 / 任何走 WS 订阅实时事件（`voicemail.new` `voicemail.played` `intercom.call` `device.online/offline/active` …）的客户端。

## 1. 现象（实测）

小程序 `wx.connectSocket` 连 `wss://<your-domain>/api/v1/events` 失败：

```
[events] 连接 WS: wss://<your-domain>/api/v1/events
[events] WS 错误: {errCode: 1004, errMsg: "open fail: _code:20,_msg:Invalid HTTP status."}
```

- 与早期 `WebSocket connection failed`（端点未部署、连不上）不同：本次**已摸到服务器**，但升级握手**未返回 101**，而是非 101 状态 → 微信报 `Invalid HTTP status`。
- 非 101 的来源是后端 `api_guard` 在 WS 升级前鉴权失败，返回了普通 **401 JSON 响应**（不升级）。

## 2. 根因（已用代码验证，非猜测）

鉴权链：
`events_routes.handle_events` → `api_guard(request)` → `get_current_user` → 注入的验证器 `verify_mini_program_user(request)`。

`verify_mini_program_user` **只从 `Authorization: Bearer <jwt>` 请求头取 token**：

```python
auth = request.headers.get("Authorization", "")
if not auth.startswith("Bearer "):
    return None          # → api_guard 回 401 → 非 101
```

而**微信小程序 `wx.connectSocket` 在 WS 升级阶段不转发自定义请求头（`Authorization` 被丢弃）**，
导致后端拿不到 token → 401 → 非 101 → 前端 1004。

反例佐证：`web/device-panel/app.js:267` 一直用 `?token=` 查询参数连同一端点，
说明这端契约本就该支持查询参数鉴权——只是后端此前只认 Header，缺口一直存在。

> 注：这不是"网关 nginx 不透传 Authorization"的模糊归因，而是更具体、可证伪的：
> 后端 events 鉴权只认 Header、不认 Query；而微信 WS 又发不了 Header。两端契约错位。

## 3. 修复（已落地）

### 3.1 后端（`fuguoai_core/utils/user_auth.py`）
`verify_mini_program_user` 增加 `?token=` 查询参数兜底（Header 仍优先，向后兼容）：

```python
def verify_mini_program_user(request) -> dict:
    auth = request.headers.get("Authorization", "")
    token = ""
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.query.get("token", "") or ""   # WS 无法带 Header 时的兜底
    if not token:
        return None
    return verify_token(token)
```

- 对 Header 客户端（浏览器 REST / device-panel 之外的普通请求）**零行为变化**。
- 安全性不变：两种来源共用同一 JWT 校验（`verify_token` 验签名/过期）。

### 3.2 前端（`services/events.js`）
握手 URL 加 `?token=<jwt>`，`Authorization` 头保留作兼容：

```js
const base = `${config.GW_BASE_URL.replace(/^https/, 'wss')}/events`
const token = wx.getStorageSync('token') || ''
const url = token ? `${base}?token=${encodeURIComponent(token)}` : base
_socket = wx.connectSocket({ url, header: token ? { Authorization: `Bearer ${token}` } : {} })
```

## 4. 契约（最终）

| 客户端 | 推荐鉴权方式 | 说明 |
|---|---|---|
| 微信小程序（WS） | `wss://.../events?token=<jwt>` | 唯一可靠方式（Header 不转发） |
| 浏览器 / web 面板（WS） | `?token=<jwt>` 或 `Authorization: Bearer` 均可 | 后端两者都认 |
| REST API | `Authorization: Bearer <jwt>` | REST 请求头正常转发，不受影响 |

## 5. 验收标准

1. 小程序连 `wss://<your-domain>/api/v1/events?token=...`：
   - DevTools network 中 WS 状态 = **101 Switching Protocols**；
   - `onOpen` 触发，`[events] WS 已连接` 打印；
   - 收到 `{"type":"hello",...}` 首帧。
2. 故意传错/空 token：后端回 401（非 101），前端 `onError` 进入节流（不再刷屏）。
3. 触发一次留言/对讲呼叫：小程序实时收到 `voicemail.new` / `intercom.call` 事件（无需手动下拉刷新）。

## 6. 影响范围

- 仅影响**实时推送**（事件 WS）；REST 拉列表（`GET /voicemail`、`GET /reminders` 等）不受影响，留言/提醒/对讲功能本身可用，只是少了实时提醒。
- 不影响设备 8000 WS、对话/ASR/TTS 路径、对讲声学（红线 3/4 严守）。
- 改动属**扩展**（新增 Query 鉴权来源），非破坏既有契约。

## 7. 改动文件

- `fuguoai_core/utils/user_auth.py`（`verify_mini_program_user` 加 `?token=` 兜底）
- `services/events.js`（小程序：URL 加 `?token=`，Header 保留）
- `docs/EVENTS_WS_COORDINATION.md`（本文件）
