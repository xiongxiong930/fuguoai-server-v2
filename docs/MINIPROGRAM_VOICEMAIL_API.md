# 小程序对接文档：离线语音留言 + 对讲质量指标

> 分支：`feature/tier1`（后端已全量测试通过、未部署；待明天对讲 A/B 联调后评审 → 合 main → 部署 159）
> 适用后端：fuguoai-server HTTP 端口 `8002`，路径前缀 `/api/v1`
> 设备固件：**零改动**（留言走服务端虚拟对端 `vm-server` 播放，复用既有 SetIntercom 下发链路）

本文档给小程序侧配置/对接点，覆盖两个新功能：
1. **离线语音留言**（VOICEMAIL）
2. **对讲质量指标 API**（INTERCOM_STATS，诊断/调试用，可选接入）

---

## 1. 通用约定

| 项 | 说明 |
|----|------|
| Base URL | `https://<网关域名>/api/v1`（与现有对讲接口同一网关、同一前缀） |
| 鉴权 | `Authorization: Bearer <user_token>`（走 `api_guard`，与现有对讲 `POST /intercom/call` 完全一致） |
| 设备 ID | 后端自动归一化（大小写/分隔符），小程序传原始 MAC 即可 |
| 实时事件 | 复用既有 `GET /api/v1/events` WebSocket，新增事件类型按 `type` 分支处理 |
| CORS | 留言接口已开 `*`（`Access-Control-Allow-Origin`），H5 调试可直接跨域 |

> 所有留言接口的设备归属校验与对讲接口同款：仅当前登录用户**名下绑定设备**可查/可传/可删。非本人设备返回 `404 not_found`。

---

## 2. 离线语音留言 API

### 2.1 上传留言 `POST /api/v1/voicemail`

**multipart/form-data**，两个字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `meta` | JSON 字符串 | `{"target_device_id":"AA:BB:CC:DD:EE:FF","source_fmt":"pcm"}` |
| `audio` | 二进制 | 录音原始字节（见下方格式约束） |

- `target_device_id`：**必填**，留言目标设备（接收方）
- `source_fmt`：音频格式标识
  - `"pcm"`（默认）：已是 **16k / 单声道 / s16le 裸 PCM**，服务端直通
  - 其他（`"wav"` / `"mp3"` / `"aac"` / `"m4a"` …）：服务端用 PyAV 解码并重采样到 16k 单声道

**格式约束（服务端强制）**：

| 约束 | 阈值 | 超限返回 |
|------|------|----------|
| 单条时长 | `0.2s` ~ `60s` | `too_short` / `too_long`（400） |
| 上传体积 | `<= 2 MB` | `too_large`（413） |
| 每设备配额 | `<= 20` 条 | `quota_exceeded`（409） |
| 保留时效 | `7` 天自动清理 | — |

> **小程序录音建议**：用 `recorderManager` 录 `format: 'PCM'`、`sampleRate: 16000`、`numberOfChannels: 1`，把得到的 `ArrayBuffer` 直接作为 `audio` 字段、`source_fmt:"pcm"` 上传，链路最短、零转码。若录的是 WAV，传 `source_fmt:"wav"` 即可。

**成功响应** `200`：
```json
{ "ok": true, "data": { "msg_id": "vm_3f9a1b2c4d5e", "duration_ms": 8420 } }
```

**错误响应**：
```json
{ "ok": false, "error": "too_long", "message": "留言时长须在 0.2s ~ 60s 之间" }
```
错误码清单：`bad_request`(缺字段/非 multipart) · `not_found`(目标设备无权限) · `bad_audio`(解码失败) · `too_long`/`too_short` · `too_large` · `quota_exceeded`

> 上传成功后，若目标设备**当前在线且空闲**，服务端会立即尝试自动投递播报（无需小程序轮询）；离线设备上线后由投递 worker 自动补播。

---

### 2.2 留言列表 `GET /api/v1/voicemail/list`

**Query 参数**：

| 参数 | 必填 | 说明 |
|------|------|------|
| `device_id` | 是 | 查询哪台设备的留言箱 |
| `status` | 否 | 按状态过滤：`new` / `played` / `read` / `deleted` |
| `limit` | 否 | 默认 `20`，上限 `100` |
| `offset` | 否 | 默认 `0` |

**成功响应** `200`：
```json
{
  "ok": true,
  "data": [
    {
      "msg_id": "vm_3f9a1b2c4d5e",
      "from": "phone-openidxxxx",
      "to": "AA:BB:CC:DD:EE:FF",
      "created_at": 1753670400,
      "duration_ms": 8420,
      "status": "new",
      "source_fmt": "pcm"
    }
  ]
}
```
> 列表按 `created_at` **倒序**（最新在前）。`status` 状态机：`new` → `played`(设备播完) → `read`(用户在 App 侧标记已听) → `deleted`。

---

### 2.3 拉取音频 `GET /api/v1/voicemail/{msg_id}/audio`

**Query 参数**：`device_id`（必填，归属校验）

- 返回 `Content-Type: audio/wav` 的 WAV 字节流（服务端把 `.fgvm` 容器里的 opus 帧**实时解码转 WAV**）
- 小程序可直接喂给 `wx.createInnerAudioContext()` 播放，**无需本地解码**

```
GET /api/v1/voicemail/vm_3f9a1b2c4d5e/audio?device_id=AA:BB:CC:DD:EE:FF
→ 200 audio/wav  (16k / mono / s16le)
```
错误：`404 not_found`（留言不存在/无权限）。

---

### 2.4 标记已读 `POST /api/v1/voicemail/{msg_id}/read`

**Query 参数**：`device_id`（必填）

> 注：该接口 `device_id` 走 **query 参数**（非 body），调用时拼在 URL 上即可。

**成功响应** `200`：`{ "ok": true }` ｜ 错误：`404 not_found`

---

### 2.5 删除留言 `DELETE /api/v1/voicemail/{msg_id}`

**Query 参数**：`device_id`（必填）

**成功响应** `200`：`{ "ok": true }`

---

### 2.6 实时事件（复用 `/api/v1/events` WS）

小程序**已在订阅** `GET /api/v1/events`（对讲 `intercom.call` 事件同源）。留言功能新增两类事件，按 `type` 分支增加即可：

**`voicemail.new`**（新留言到达，可触发红点/ toast）：
```json
{
  "type": "voicemail.new",
  "msg_id": "vm_3f9a1b2c4d5e",
  "device_id": "AA:BB:CC:DD:EE:FF",
  "from": "phone-openidxxxx",
  "duration_ms": 8420,
  "ts": 1753670400
}
```

**`voicemail.played`**（设备端播放完成，可更新列表状态）：
```json
{
  "type": "voicemail.played",
  "msg_id": "vm_3f9a1b2c4d5e",
  "device_id": "AA:BB:CC:DD:EE:FF",
  "ts": 1753670460
}
```

> 事件经事件总线推送，按用户设备归属过滤；与对讲事件同一条 WS，无订阅者零开销。

---

## 3. 对讲质量指标 API（诊断/调试用，可选接入）

把之前"落盘定罪"的诊断资产产品化为常驻 HTTP 指标，纯内存零持久化。

### 3.1 活跃会话 `GET /api/v1/intercom/stats`

返回当前进行中的对讲通话实时指标；无活跃会话返回 `[]`。

**鉴权双口径**：
- **Admin**：`Authorization: Bearer <FUGUOAI_ADMIN_TOKEN>` → 看全部会话（运维/客服后台用）
- **普通用户**：`api_guard` 登录态 → 只看自己名下设备相关的会话

### 3.2 最近通话快照 `GET /api/v1/intercom/stats/recent?limit=20`

返回最近 `limit`（默认 20，上限见服务端 `RECENT_KEEP`）通已结束通话的终态快照，用于回看/客诉定位。

### 3.3 响应字段说明

```json
{
  "ok": true,
  "data": [
    {
      "sid": "call_abc123",
      "a": "phone-openidxxxx",
      "b": "AA:BB:CC:DD:EE:FF",
      "mode": "phone-device",
      "started_at": 1753670400,
      "age_s": 42.3,
      "up":   { "frames": 2115, "bytes": 677000, "errs": 0, "fps_10s": [49.8, 50.1, 50.0], "stall": false },
      "down": { "frames": 2102, "bytes": 672000, "errs": 0, "fps_10s": [49.9, 50.0, 49.7], "stall": false },
      "rt_ratio": 1.0,
      "ended_at": 1753670442,
      "end_reason": "hangup"
    }
  ]
}
```

| 字段 | 含义 |
|------|------|
| `mode` | `phone-device`（手机↔设备）或 `device-device`（设备↔设备） |
| `up` / `down` | 方向：`up`=手机发出/设备A→B；`down`=发往手机/设备B→A |
| `frames` | 该方向成功转发的 20ms 帧数 |
| `bytes` | 转发字节数 |
| `errs` | 编/解码失败计数（异常必查） |
| `fps_10s` | 最近若干个 10s 桶的帧率滑窗（≈50 = 正常；骤降=上行被压） |
| `stall` | 该方向是否出现 >3s 静默断流（true=卡顿） |
| `rt_ratio` | 实时率 = 上行音频时长 / 会话墙钟（≈1.0=零堆积；远低于1=严重堆积/丢帧） |
| `end_reason` | `hangup` / `peer_left` / `peer_offline` / `teardown` / `grace_timeout` |

> **运维判读**：`rt_ratio` 掉到 0.3 量级 + `up.fps_10s` 骤降 + `stall:true` ≈ 上行被压制（参考 07-28 的 60ms 事故）；`errs>0` 需查编解码。

---

### 3.4 `intercom.call` 事件新增字段：`can_leave_message`

呼叫编排器在**呼叫失败且原因为对端离线**时，向小程序推送的 `intercom.call` 事件会附带 `can_leave_message: true`（只增字段，老客户端无感）。

```json
{
  "type": "intercom.call",
  "call_id": "call_abc123",
  "status": "failed",
  "reason": "peer_offline",
  "device_id": "AA:BB:CC:DD:EE:FF",
  "peer_device_id": "11:22:33:44:55:66",
  "ts": 1753670400,
  "can_leave_message": true
}
```

> 小程序据此在「呼叫失败 / 对方离线」界面展示 **「留语音消息」** 入口，跳转到 §2.1 上传页（预填 `target_device_id = peer_device_id`）。

---

## 4. 小程序侧改动清单（Checklist）

| # | 改动点 | 说明 |
|---|--------|------|
| 1 | **录音入口** | 新增「给设备留语音」录制页，`recorderManager` 录 `PCM / 16k / mono` |
| 2 | **上传** | `POST /api/v1/voicemail`，`multipart(meta+audio)`，`source_fmt:"pcm"` |
| 3 | **留言列表页** | `GET /list` 倒序展示；`status:"new"` 显示红点/未读标记 |
| 4 | **播放** | 点击 → `GET /{msg_id}/audio` 拿 WAV 直接 `InnerAudioContext` 播；播完调 `read` 标记已读 |
| 5 | **已读/删除** | `POST /read?device_id=` · `DELETE /{msg_id}?device_id=` |
| 6 | **events WS** | `onMessage` 增加 `case "voicemail.new"`（红点/ toast）、`case "voicemail.played"`（刷新列表状态） |
| 7 | **对讲失败界面** | 收到 `intercom.call` 且 `can_leave_message:true` → 显示「留语音消息」跳转 |
| 8 | **（可选）指标页** | 客服/调试页拉 `/intercom/stats`、`/intercom/stats/recent`，展示 `rt_ratio`/`stall`/`errs` |

---

## 5. 部署状态与注意事项

- **当前状态**：后端代码全部在 `feature/tier1` 分支（commit `abd90cb`），回归门禁 **ALL PASS**（L1+L2+L3，43 新断言），**尚未部署到 159 容器**。
- **上线路径**：明天对讲 A/B 联调 → tier1 功能评审 → 合 `main` → 单文件部署脚本固化上 159。
- **固件零改动**：留言播放由服务端冒充虚拟对端 `vm-server`，按 20ms 实钟节奏下发，设备端无需任何固件改动（复用现有剥头解码 + SetIntercom 链路）。
- **配额/时效**：每设备 ≤20 条、保留 7 天、单条 0.2s~60s、上传 ≤2MB；超配额返回 `quota_exceeded`，前端需提示用户删除旧留言。
- **指标 API 权限**：普通用户仅看自己设备；Admin 令牌看全局，勿在前端硬编码 Admin 令牌。

---

## 附录：最小对接示例（伪代码）

```js
// 1) 上传留言
const fs = wx.getFileSystemManager();
wx.request({
  url: `${BASE}/api/v1/voicemail`,
  method: 'POST',
  header: { Authorization: `Bearer ${userToken}` },
  formData: { meta: JSON.stringify({ target_device_id: devId, source_fmt: 'pcm' }) },
  filePath: tempPcmPath,        // recorderManager 录出的 PCM 临时文件
  name: 'audio',
  success: (r) => console.log('留言已提交', r.data.data.msg_id),
});

// 2) 事件 WS 增量分支
ws.onMessage((e) => {
  const ev = JSON.parse(e.data);
  if (ev.type === 'voicemail.new')   showRedDot(ev.device_id);
  if (ev.type === 'voicemail.played') refreshList(ev.device_id);
  if (ev.type === 'intercom.call' && ev.can_leave_message)
    showLeaveMessageEntry(ev.peer_device_id);
});

// 3) 呼叫失败跳留言
// 在 intercom.call 失败界面：target_device_id = ev.peer_device_id → 进入录制页
```
