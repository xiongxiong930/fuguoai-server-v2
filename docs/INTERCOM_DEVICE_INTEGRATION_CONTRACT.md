# 对讲（Intercom）设备端对接契约 — 后端交付

> **受众**：接入复果AI 后端、需要实现设备侧对讲能力的固件开发方。
> **交付方**：后端（`fuguoai-server`）。本文所有条目均从**当前运行代码**核实，非设计稿。
> **版本**：2026-08-02（对应后端基线）。
>
> 本文是与后端对接的**对讲接口契约**；§8 汇总对接时务必留意的已知坑。

---

## 0. TL;DR — 三句话

1. 设备在 **hello 里带 `"session": "intercom"`**，该 WS 连接即进入透传模式，后端不跑 VAD/ASR/LLM/TTS。
2. 音频走**同一条 WS 的二进制帧**，内容是**裸 opus**（16kHz / 单声道 / 20ms / 无任何头字节）。
3. 开关由后端下发 IoT 指令 **`DeviceControl.SetIntercom{peer, action}`** 控制。

**最大的坑（先看 §6）**：不要为对讲另开一条 WS。后端注册表按 `device_id` 单槽覆盖，第二条 WS 会把第一条的下行通道顶掉。

---

## 1. 链路总览

```
                    ┌──────────── 控制面（JSON 文本帧）────────────┐
                    │                                              │
  小程序/HTTP ──POST /api/v1/devices/{id}/intercom──> 后端编排      │
                                                        │           │
                                                        ├─ SetIntercom{peer,start} ─> 设备
                                                        └─ intercom 事件 ──────────> 设备
                    ┌──────────── 数据面（二进制帧）──────────────┐
                    │                                              │
  设备 A ──裸 opus──> relay ──裸 opus──> 设备 B      （设备↔设备：零转码原样透传）
  设备   ──裸 opus──> relay ──640B PCM─> 手机        （设备↔手机：relay 边界转码）
  手机   ──640B PCM─> relay ──裸 opus──> 设备
```

- **控制面与数据面共用同一条 WS**（文本帧 = 控制，二进制帧 = 音频）。
- 后端对设备↔设备**不做任何转码**，收到什么原样转发什么。所以两台设备的编码参数必须一致。
- 设备↔手机由后端 relay 在边界转码（PyAV，纯内存，不起 ffmpeg 进程）。

---

## 2. hello 契约 — 进入透传模式的唯一开关

### 2.1 设备上行 hello

```json
{
  "type": "hello",
  "version": 1,
  "session": "intercom",
  "fw_version": "<version>",
  "audio_params": {
    "format": "opus",
    "sample_rate": 16000,
    "channels": 1,
    "frame_duration": 20
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `session` | **是** | 值为 `"intercom"` 时该连接进透传模式。**这是唯一开关**（`helloHandle.py:125`）。不带此字段 = 普通对话连接，行为与现在完全一致。 |
| `audio_params.sample_rate` | **是** | 必须 `16000`。见 §3 硬约束。 |
| `audio_params.frame_duration` | **是** | 必须 `20`。**对话链路通常用 60ms，对讲必须单独改为 20ms**，见 §8.1。 |
| `audio_params.channels` | 是 | `1` |
| `audio_params.format` | 是 | `"opus"` |
| `fw_version` | 建议 | 后端日志会打 `[device_id][fw_version]`，联调归因用。 |

> `device-id` 走 WS 握手 HTTP header（与现有对话连接一致），不在 hello body 里。

### 2.2 后端在透传模式下强制覆盖的项（`helloHandle.py:210-215`）

进入 `intercom_mode` 后，后端**无条件**执行：

```python
conn.mic_ts_header = False   # 强制关闭 8B 小端 ts 头
conn.client_aec    = False   # 后端不参与 AEC
```

**含义**：
- 若设备侧在对话链路使用 8 字节 ts 头（`mic_ts_header=1`），**对讲连接上必须不发**。后端已强制按"无头"解析，你若仍前置 8 字节，opus 解码会直接失败（落 `transcode_fail`）。
- 对讲的 AEC 完全在**设备侧**做，后端不下发任何参考流、不做服务端 AEC。

另外 `features`（含 `mcp` / `aec`）在 intercom 连接上**会被忽略**（`helloHandle.py:185`：`if features and not intercom_mode`）。

### 2.3 后端 GREET 回包

```json
{
  "type": "hello",
  "version": 1,
  "transport": "websocket",
  "session_id": "<后端生成>",
  "audio_params": { ...原样回显你上报的 audio_params... }
}
```

**注意回声语义**（`helloHandle.py:178`）：`audio_params` 是把你上报的原样写回。
**若你不报 `audio_params`**，回包会用配置模板初值 **`24000 / 60ms`** —— 与对讲的 16k/20ms 硬约束直接冲突。所以 §2.1 里 `audio_params` 标"必填"不是形式要求。

---

## 3. 音频帧契约（数据面）

### 3.1 格式 — 无商量余地

| 项 | 值 | 依据 |
|---|---|---|
| 编码 | opus | — |
| 采样率 | **16000 Hz** | 后端 relay 解码器固定 |
| 声道 | **1** | — |
| 帧长 | **20 ms（320 样本）** | `intercom_registry` 中 `decode(opus, 320)` 写死 |
| 码率 | 建议 24 kbps | 与 conversation 侧对齐 |
| **DTX** | **必须关闭** | 见 §7.3 watchdog，静音期停发会被判死 |
| 帧头 | **无。裸 opus 净荷** | 08-02 起移除 4B seq；8B ts 头亦禁用 |

> **320 样本这条是硬失败点**：后端 `decode(opus, 320)` 若收到 60ms 帧（960 样本）会直接解码失败，落 `transcode_fail`，表现为"对方完全没声音"且日志无明显报错（已限频）。

### 3.2 收发方向

- **上行**：设备 → 后端，WS 二进制帧，一帧一个 opus packet，**50 帧/秒**恒定节奏。
- **下行**：后端 → 设备，同样是裸 opus 帧。
  - 对端是设备 → 原样透传对端的 opus。
  - 对端是手机 → 后端把手机的 640B PCM 编码成 opus 再发你。
  - **两种情况你收到的格式完全一致**，无需区分对端类型。

### 3.3 节奏

按 **20ms 实钟**逐帧发送，不要攒批。后端 relay 是逐帧即时转发、零队列（`_timing_note` 会统计实时率，≈1.0 为正常）。上游突发灌入会在对端造成抖动。

---

## 4. 控制指令 — `SetIntercom`

### 4.1 后端下发原文（`intercom_call_store.py:74-83`）

```json
{
  "type": "iot",
  "commands": [
    {
      "name": "DeviceControl",
      "method": "SetIntercom",
      "parameters": { "peer": "<peer_device_id>", "action": "start" }
    }
  ]
}
```

`action` 取值：`"start"` | `"stop"`。

### 4.2 固件侧需要做什么

设备固件侧需新增 `DeviceControl` thing 并注册 `SetIntercom` 方法（若尚未实现）。

收到 `start` 时的动作（建议）：
1. 切换音频管线到对讲模式（16k/20ms 编解码器、关 DTX、启用设备侧 AEC）。
2. 建立/复用 intercom 透传连接（见 §6 —— 强烈建议复用单条 WS 而非新开）。
3. 开始以 50 帧/秒恒定上行。

收到 `stop`：停止上行、恢复对话管线。

> **`stop` 不只在用户挂断时来**。后端 watchdog 判定"设备假在线"时也会主动下发 `stop`（§7.3）。固件必须把 `stop` 当作**权威指令**无条件执行，不要因为"我这边还在通话中"而忽略。

### 4.3 后端何时下发

| 时机 | 触发 |
|---|---|
| 呼叫接通 | 双方均在线 → 低层建会话 → 向**设备端**下发 `start`（手机端不下发） |
| grace 内重连 | 设备带 `session:intercom` 重连并命中 grace 会话 → **重发一次** `start`（`helloHandle.py:234`） |
| 首连自举 | connected 后设备上行塌陷 → 重发 `start`，**最多 2 次**（§7.2） |
| 看门狗收敛 | 判定设备假在线 → 下发 `stop`（§7.3） |
| 主动挂断 | HTTP `stop` / 对端 leave |

**注意**：`start` 可能**重复到达**（自举/grace 重发）。固件必须做幂等 —— 已在对讲中再收到同 peer 的 `start`，应视为"确认仍需保持"，不要重建管线或重连。

---

## 5. 事件（文本帧，后端 → 设备）

```json
{ "type": "intercom", "event": "<event>", "peer": "<device_id>" }
```

| event | 含义 | 建议动作 |
|---|---|---|
| `established` | 会话建立 | 开始收发音频 |
| `peer_offline` | 对端不在线 | 提示 + 收敛 |
| `peer_left` | 对端离开（含 grace 超时） | 结束对讲，恢复对话模式 |
| `peer_busy` | 对端忙线 | 提示 |

---

## 6. 🔴 连接模型 — 单 WS 还是双 WS（最重要的选型）

### 6.1 后端事实

`connection_registry` 的三张表都是 **`device_id` 单槽**，写入即覆盖：

```python
# connection_registry.py:87
self._online[device_id] = {...}    # 直接覆盖
# connection_registry.py:213
self._send[device_id]   = {...}    # 直接覆盖
```

**后果**：同一 `device_id` 开第二条 WS 时，第一条的 `_online` / `_send` / `_kick` 登记**立刻被顶掉**。后端此后所有下行（TTS 音频、IoT 指令、SetIntercom、事件）都只会发往**最后建立的那条**连接。第一条 WS 在后端眼里"还连着，但收不到任何下行"。

### 6.2 两个方案的取舍

| | 方案 A：单 WS 模式切换（**推荐**） | 方案 B：双 WS 并存 |
|---|---|---|
| 做法 | 对讲时断开主 WS，重连并在 hello 带 `session:intercom`；结束后再切回 | 保留主对话 WS，另开一条 intercom WS |
| 注册表冲突 | **无** | **有** — 主 WS 失去全部下行能力 |
| session_id 复用坑 | **无** | 有（实践中已踩，见 §8.2） |
| 切换延迟 | 一次建链（~0.5–1s） | 无 |
| 实现复杂度 | 低 —— 复用现有连接生命周期 | 高 —— 需管理两条连接的状态机、资源、心跳 |
| 双 WS 实践 | — | **已证实出问题**（双端无声，排查两周） |

### 6.3 推荐：方案 A

理由不只是"避坑"，而是**后端语义本就是单连接单模式**：`intercom_mode` 是连接级属性（`conn.intercom_mode`），在 hello 时一次性确定，连接存续期内不可变。双 WS 是在跟后端的连接模型对抗。

**方案 A 的时序**：

```
[对话模式] 主 WS 连着
    ↓ 收到 SetIntercom{peer, start}
主动 close 主 WS
    ↓
重连，hello 带 {"session":"intercom", audio_params:{16000, 20ms}}
    ↓ 收到 GREET + intercom established
[对讲模式] 同一条 WS 收发裸 opus
    ↓ 收到 SetIntercom{stop} 或 peer_left
主动 close
    ↓
重连，hello 不带 session 字段
[对话模式] 恢复
```

**唯一注意点**：`SetIntercom start` 是在**主 WS** 上收到的，收到后你会主动断开这条 WS。后端对此有兜底 —— grace 内（30s）带 `session:intercom` 重连会恢复会话并**自动重发一次 start**（`helloHandle.py:234`）。所以断开重连不会丢会话。

> 若因产品原因必须保持主对话 WS 常连（例如对讲中仍要能被唤醒），那就得走方案 B，但**需要后端先支持 `device_id` 多连接注册**（把单槽改成按 `conn_uid` 索引的多槽 + 下行路由策略）。这是后端改造，不是固件能绕过的。**需要就提，我来做。**

---

## 7. 服务端时序行为 — 固件必须知道，否则会被"莫名 stop"

这一节的参数都在 `intercom_call_store.py:38-62` 和 `intercom_registry.py:87`。

### 7.1 grace（断连宽限）— `GRACE_SEC = 30.0`

WS 断开后，后端**不立即拆会话**，标记 grace 并起 30s 定时器：
- 30s 内同 `device_id` 带 `session:intercom` 重连 → **恢复原会话**，对端无感，并重发一次 `SetIntercom start`。
- 超时 → 对端收 `peer_left`，会话拆除。
- grace 期间对端发来的帧**被丢弃**（`drop{reason=session_not_established}`）。

### 7.2 bootstrap（首连自举）

connected 后每 `1.5s` 体检一轮，最多 8 轮（≈12s 窗口）：
- 条件：设备上行静默 > `1.2s` **且** 手机最近 `2.0s` 内在发帧
- 动作：重发 `SetIntercom start`，**最多 2 次**

目的是救"设备收到 start 但没真正开起来"的首连死。**对固件的含义**：收到重复 `start` 要幂等（§4.2）。

### 7.3 🔴 watchdog（上行静默看门狗）— 会主动拆你的会话

connected 满 `6.0s` 免检期后，每 `1.0s` 巡检。**三个条件同时满足**才动手：

1. connected 已满 6s（把自举的 2 次机会让完）
2. 设备上行静默 > **5.0s**
3. 手机侧最近 5s 仍在发帧（证明下行确实在成功写入）

动作：判定设备"假在线" → 下发 `SetIntercom stop` + 向手机推 `peer_offline` → 收敛通话。

> **这就是为什么 §3.1 要求 DTX 必须关闭。** 对讲场景下"零上行"被定义为链路死，**不等于**"用户没说话"。设备即便完全静音也必须持续发帧（50 帧/秒）。若你开了 DTX，用户安静 5 秒钟通话就会被后端主动掐断。

### 7.4 呼叫振铃 — `CALL_TTL = 30.0`

发起呼叫后 30s 内对方未上线接通 → 呼叫单超时作废。

---

## 8. 已知坑清单（对接时务必留意）

### 8.1 帧长 60ms ≠ 20ms

对话链路通常用 `60ms`（如 `OPUS_FRAME_DURATION_MS = 60`），对讲**必须 20ms**，且后端 `decode(opus, 320)` 写死。

**这意味着对讲需要一套独立的编解码器实例**，不能复用对话那套。另外 §0 已实证 60ms 在对讲链路上有害（累积延迟 + AEC 对齐失效）。

### 8.2 双 WS 复用同一 session_id

若主对话 WS 与 intercom WS **复用了同一个 session_id**，会导致后端"旧连接迟到断开"时按 session_id 误删活跃新连接的登记 → 会话被拆。

后端已用 `conn_uid`（每条 WS 唯一，服务端内部生成，不上协议）做容错，**固件无感、无需配合**。但方案 A 从根上不会遇到这个问题。

### 8.3 上行"一次失败即永久瘫痪"（H5，最高嫌疑）

ESP-IDF 类基线的 TCP 传输层（如 `tcp_transport.cc`）：

```cpp
int TcpTransport::Send(...) {
    int ret = send(fd_, data, length, 0);
    if (ret <= 0) { connected_ = false; }   // 任何 ret<=0 一律判死
}
```

`connected_` 除 `Connect()` 外**全代码无复位路径**；配合 `web_socket.cc:369` 的 `while (transport_->connected() && len > 0)` ⇒ **一次瞬时 send 失败就永久瘫痪上行**，而 TCP 其实还活着。

**为什么只有对讲触发**：对讲上行是恒定 50 帧/秒，主对话是突发 + VAD 门控。只有前者会瞬时撑爆 lwIP `TCP_SND_BUF`/pbuf 池，拿到 `EAGAIN`（`ret < 0`）。

**做对讲前建议先修这里**：至少要区分 `EAGAIN`/`EWOULDBLOCK`（可重试，不应判死）与真正的致命错误。否则对讲跑起来（恒定 50 帧/秒上行）大概率复现同样的"上行归零"。

### 8.4 errno 映射陷阱

ESP-IDF 用 **newlib**，errno 值与 Linux **不同**：
- newlib `119` = `EADDRNOTAVAIL`（**不是** `ECONNABORTED`，后者是 `123`）
- newlib `128` = `ENOTCONN`
- newlib `11` = `EAGAIN`

排查时照抄 Linux errno 表会把方向完全带偏。

---

## 9. 自测步骤（建议按序）

### 9.1 第一步：loopback 自环（单板，不需要第二台设备）

```bash
curl -X POST "https://<host>/api/v1/devices/<your_device_id>/intercom" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"action":"start","peer":"loopback"}'
```

- `peer:"loopback"` = 自环，后端把你的上行原样回发给你自己。
- **不查在线、不占忙线**，是验证"编解码 + 收发管线"最干净的方式。
- 预期：说话后从自己喇叭听到自己的声音（有一个 RTT 的延迟）。
- 这一步过了，说明帧格式、采样率、帧长、收发路径全对。

停止：
```bash
-d '{"action":"stop","peer":"loopback"}'
```

### 9.2 第二步：设备 ↔ 设备

两台板子都跑新固件，对其中一台发 `{"action":"start","peer":"<另一台 device_id>"}`。
此路径**零转码**，能验证纯透传链路。

### 9.3 第三步：设备 ↔ 手机（小程序）

此路径经过 relay 边界转码（opus ↔ PCM），是最完整但也最容易暴露参数不匹配的一条。

---

## 10. 可观测 — 出问题先看 `/metrics`

后端 8002 端口 `/metrics`（无鉴权，内网）。对讲相关三个计数器：

| 指标 | 含义 |
|---|---|
| `fuguoai_intercom_relay_uplink_total{from,to}` | 成功转发到手机的帧（**设备→手机**）。**为 0 = 设备根本没上行** |
| `fuguoai_intercom_relay_downlink_total{from,to}` | 成功转发到设备的帧（手机→设备 / 设备→设备）。>0 = 后端已写入设备 WS |
| `fuguoai_intercom_relay_drop_total{reason}` | 丢弃帧，按原因分类 |

### `drop.reason` 语义速查（定位神器）

| reason | 含义 | 多半是什么问题 |
|---|---|---|
| `not_registered` | 来源设备不在 `_by_device` 表 | 没进透传模式（hello 漏了 `session:intercom`），或会话已被拆 |
| `session_not_established` | 会话处于 grace / ringing 期 | 对端断连中，或还没接通 |
| `peer_offline` | 对端无活跃连接 | 对端掉线，或**被同 device_id 的第二条 WS 顶掉了**（§6.1） |
| `transcode_fail` | 解码失败 | **帧格式不对** —— 帧长/采样率错，或误加了帧头 |
| `send_error` | 写对端 WS 失败 | 对端 socket 真的断了 |
| `vm_sink` | 留言播放中静默丢弃 | 正常行为 |

> **两条最有用的判读**：
> - `uplink=0` 而 `downlink` 很大 ⇒ 后端在正常发给你，是**设备上行没出去**（看 §8.3）。
> - `transcode_fail` 持续增长 ⇒ **帧参数错**，回头核对 §3.1。

日志侧另有 `[intercom][debug] relay到达: <src> len=<n> head=<hex>`，每 2s 一条，`head` 是帧首 8 字节 hex —— 可肉眼确认是不是裸 opus（无 seq、无 ts 头）。

---

## 11. 验收清单

对讲功能可交付的判定条件：

- [ ] hello 带 `session:intercom`，后端日志出现 `[intercom] 连接进入对讲透传模式`
- [ ] `audio_params` 上报 `16000 / 1 / 20`，GREET 回包回显一致（**不是** 24000/60）
- [ ] loopback 自环能听到自己
- [ ] 上行恒定 50 帧/秒，`uplink_total` 稳定增长
- [ ] `drop_total{transcode_fail}` 恒为 0
- [ ] 静音 10 秒不断连（验证 DTX 已关、未被 watchdog 收敛）
- [ ] 重复收到 `SetIntercom start` 不重建管线（幂等）
- [ ] 收到 `SetIntercom stop` 无条件收敛
- [ ] 断网 10s 后重连（grace 内）会话自动恢复，对端无感
- [ ] 对讲结束后能干净切回对话模式，TTS 下行正常

---

## 12. 后端可配合的改造（按需提）

以下是固件绕不过、需要后端动的：

1. **`device_id` 多连接注册**（若必须走方案 B 双 WS）：把 `_online`/`_send`/`_kick` 从单槽改为按 `conn_uid` 索引的多槽，并定义下行路由策略（IoT 走哪条、TTS 走哪条）。
2. **watchdog 阈值可配**：当前 `WATCHDOG_SILENCE_SEC = 5.0` 硬编码，若固件有合理的静默场景可调。
3. **对讲帧长协商**：当前 20ms 是后端写死的，若确有必要支持其他帧长可改成按 hello 协商。

有需要直接说，这三项都在后端侧，我来改。

---

**附：关键代码位置索引**（后端，便于对照）

| 内容 | 位置 |
|---|---|
| intercom 模式识别 | `fuguoai_core/handle/helloHandle.py:125` |
| 强制关 ts 头 / AEC | `fuguoai_core/handle/helloHandle.py:210-215` |
| grace 重连重发 start | `fuguoai_core/handle/helloHandle.py:234` |
| 帧转发热路径 relay | `fuguoai_core/utils/intercom_registry.py:245` |
| grace 参数 | `fuguoai_core/utils/intercom_registry.py:87` |
| SetIntercom 指令构造 | `fuguoai_core/utils/intercom_call_store.py:74-83` |
| bootstrap / watchdog 参数 | `fuguoai_core/utils/intercom_call_store.py:38-62` |
| HTTP 发起入口 | `fuguoai_core/api/devices_routes.py:535` |
| 注册表单槽覆盖 | `fuguoai_core/utils/connection_registry.py:87, 213` |
