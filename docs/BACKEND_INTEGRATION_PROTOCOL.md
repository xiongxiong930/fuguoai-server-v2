# 复果AI 后端对接协议（固件团队接入参考）

> **受众**：实现设备侧固件的团队（按本契约让设备接入复果AI 后端）。
> **来源**：本文所有字段、状态码、时序均从**当前运行代码**逐文件核实，非设计稿。
> **配套文档**：对讲设备端契约见 `docs/INTERCOM_DEVICE_INTEGRATION_CONTRACT.md`；事件 WS 见 `docs/EVENTS_WS_COORDINATION.md`。
> **版本**：2026-08-12。

---

## 0. 部署端点与端口

| 端口 | 协议 | 用途 | 备注 |
|---|---|---|---|
| **8000** | 裸 `websockets`（路径任意，如 `/fuguoai/v1/`） | 设备语音长连 + 对讲音频透传 | `config.server.port`，bare websockets，**不**走 aiohttp |
| **8002 / 8003** | aiohttp HTTP | 小程序 REST、OTA、天气、视觉、metrics、events WS | 模板 `http_port=8003`；**当前线上实际为 8002**（nginx 反代 `<your-domain>`）。以 `data/.config.yaml` 的 `server.http_port` 为准 |
| — | nginx 反代 | `wss://<your-domain>/...` 对外 | 微信小程序等不支持裸 TCP，须走 wss |

> 联调时若直接打 IP，设备语音 WS 用 `ws://<ip>:8000/fuguoai/v1/`，REST/OTA/事件用 `http(s)://<ip>:8002`。

---

## 1. 鉴权总览

| 通道 | 鉴权方式 | 失败后果 |
|---|---|---|
| **设备 WS 8000** | `device-id` HTTP 头（归一化：小写 + 去 `:`/`-`）+ `Authorization: Bearer <device_token>`（裸 token 亦可） | 握手前发 `"认证失败"` 并关闭 |
| 设备 WS（手机对讲模式 B） | hello 内带 `role:"phone"` + `token:<小程序JWT>`，**不带** device-id 头 | hello 回 `{"type":"hello","error":"auth_failed"}` 并关闭 |
| **小程序 REST（/api/v1/*）** | `Authorization: Bearer <小程序JWT>`（由 `POST /api/v1/auth/login` 换取） | 守卫返回 401 JSON，**不升级** |
| **设备侧 HTTP（OTA / weather）** | `device-id` 头（+OTA 需 `client-id`）；鉴权随绑定下发的设备凭证统一管控，详见 §1.1 | 缺 device-id → 400；token 错 → 401 |
| **事件 WS（/api/v1/events）** | `Authorization: Bearer <JWT>` **或** 查询参数 `?token=<JWT>`（微信 WS 只能走 Query） | 非 101（401），前端 `onError` |
| **运营后台（/admin）** | `Authorization: Bearer $FUGUOAI_ADMIN_TOKEN`（env 令牌） | 401 |

> **设备 id 归一化规则**（贯穿全栈，务必一致）：`normalize_device_id` = 转小写 + 去掉 `:` 与 `-`。固件上报 MAC `AA:BB:CC:...` 与小程序侧 `aabbcc...` 视为同一设备。REST 侧所有 `device_id` 入参也走该归一化。

### 1.1 接入前提（必读）

**设备要接入复果AI 后端，必须先通过「复果造物工坊」小程序完成绑定。** 绑定由小程序负责设备配网与设备凭证（`device_token`）下发；**我方暂时不对外提供自助接入 / 开放注册**，设备凭证仅由该小程序在绑定时签发，固件侧无需、也无法本地生成。（固件在 BLE 配网成功后调用 `POST /api/v1/devices/bind`，用小程序下发的 `bind_token` 兑换服务端签发的 `device_token`——固件只接收、不本地生成，详见 §6.2。）

**固件侧须满足的 BLE 配网前提（否则小程序发现不了、绑不了）：**
- BLE 广播名 = `FUGUO_<MAC 后 4 位大写>`（示例：`FUGUO_8818`）；
- 实现 `FUGUO-PROWIFI` / `FUGUO-WIFI-CFG` / `FUGUO-RESULT` GATT 服务（配网凭据下发与结果回传）。

绑定成功后，固件即可用 `device-id`（**裸 MAC**，如 `8856a6fb0ff4`）+ `device_token` 建立设备 WS（详见 §2）；设备侧 HTTP（OTA / weather）亦使用同一 `device-id` 标识。

---

## 2. 设备 WS 协议（端口 8000）

### 2.1 建连与握手

1. 客户端发起 `ws://<host>:8000/fuguoai/v1/`，带上 `device-id` 头（或 `?device-id=` 查询参数）与 `Authorization` 头。
2. 服务端先做鉴权（`websocket_server._handle_auth`）：设备令牌校验失败 → 发 `"认证失败"` 并关闭。
3. 建连后**必须尽快发送 `hello` 文本帧**，否则握手超时判定断开（`conn._hello_received`）。
4. 服务端回 **GREET** 文本帧（`config["fuguoai"]` 注入 `session_id` + 回显 `audio_params`）。

**GREET 回包示例**（来自运行配置 `data/.config.yaml` 的 `fuguoai` 块）：

```json
{
  "type": "hello",
  "version": 1,
  "transport": "websocket",
  "session_id": "<服务端生成的 uuid>",
  "audio_params": {
    "format": "opus",
    "sample_rate": 24000,
    "channels": 1,
    "frame_duration": 60
  },
  "wakeup_words": [ "你好小福", "小福小福" ]
}
```

> ⚠️ **GREET 的 `audio_params` 描述的是「下行 / TTS」参数**（即 24000Hz / 60ms），**不是**上行 mic 参数。上行 mic 固定 16000Hz（后端 ASR 硬约束，见 §2.5）。回声语义：GREET 的 `audio_params` 是把你 hello 里上报的值**原样回显**；若你不报，回包退回配置模板初值 **24000 / 60ms**——这与对讲的 16k/20ms 硬约束冲突。所以对讲连接**必须**上报 `audio_params`。

### 2.2 设备上行 `hello` 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `type` | 是 | 固定 `"hello"` |
| `session` | 否 | 值为 `"intercom"` → 该连接进**对讲透传模式**（后端不跑 VAD/ASR/LLM/TTS）。不带 = 普通对话连接 |
| `role` | 否 | `"phone"` → 小程序真人语音对讲手机端，配合 `token` + `target_device_id` |
| `token` | 否 | 手机端 JWT（仅 `role:"phone"` 时用） |
| `target_device_id` | 否 | 手机端目标设备（归一化） |
| `fw_version` | 建议 | 后端日志打 `[device_id][fw_version]`，联调归因用 |
| `audio_params` | 建议 | `{format, sample_rate, channels, frame_duration, mic_ts_header?}` |
| `features` | 否 | `{mcp: bool, aec: bool}`；**intercom 模式下被忽略** |

`audio_params.mic_ts_header`：置 `1`（v1.10.10+）表示麦流二进制帧前置 **8 字节小端 uint64 同源 ts 头**（用于服务端 AEC 对齐）。旧固件不置。

### 2.3 上行文本消息类型（共 10 种）

| `type` | 方向 | 形状 / 字段 | 说明 |
|---|---|---|---|
| `hello` | ↑ | 见 2.2 | 握手（每连接仅一次有效） |
| `listen` | ↑ | `{"type":"listen","state":"start\|stop\|detect","mode"?:"realtime","text"?:<asr文本>}` | `start`=切回录音清缓冲；`stop`=触发 ASR；`detect`+`text`=本地唤醒词/设备呼叫已识别文本（见下）；`mode:"realtime"` 开启服务端 AEC（全双工） |
| `abort` | ↑ | `{"type":"abort","reason"?:"barge_in"\|...}` | 显式打断。realtime 下仅 `reason=="barge_in"`（遗留语音打断）被忽略；其余（POWER/按键，不带 reason 或 `"button"`）**无条件停 TTS + 转聆听** |
| `iot` | ↑ | `{"type":"iot","descriptors"?:[...],"states"?:[...],"schedule"?:[...],"full"?:bool}` | 设备能力描述符 / 状态镜像 / 定时任务镜像（见 §5） |
| `mcp` | ↑ | MCP 协商帧（`features.mcp` 时由后端主动 `initialize`） | 大模型工具调用通道 |
| `server` | ↑ | `{"type":"server","action":"update_config"\|"restart","content":{"secret":...}}` | 仅 API 配置模式（`read_config_from_api`）生效，需匹配 manager-api secret |
| `ping` | ↑ | `{"type":"ping"}` | 仅 `enable_websocket_ping=true` 时回 `pong`（默认未启用） |
| `audio_playback_ref` | ↑ | `{"type":"audio_playback_ref","data":"<base64 opus>","ts":<播放时刻ms>,"rate":<int>}` | 固件播放 TTS 时的**回声参考流**，用于服务端 AEC；收到即自动开启 `client_aec` |
| `wake_word_in_session` | ↑ | 唤醒词在对话中触发 | 对话态内唤醒 |
| `speak_request` | ↑ | `{"type":"speak_request","text":"<≤200字>","req_id"?:<可选>}` | 设备在线时主动请求服务端把任意文本合成 TTS 并推流播放（提醒/播报 B 方案） |

**`listen` 的 `detect` 文本约定**：
- 以 `[device_call]` 开头 → 设备呼叫指令，后端进入来电接听模式并播报后续文本。
- 命中 `wakeup_words` → 唤醒问候（`greeting_text`，默认 `"嘿，你好呀"`）或视为普通提问走 LLM。
- 其他 → 直接作为用户 utterance 走 `startToChat`。

### 2.4 下行文本消息

| `type` | 形状 | 说明 |
|---|---|---|
| `stt` | `{"type":"stt","text":"<去标点文本>","session_id":...}` | 用户语音识别回显（字幕） |
| `tts` | `{"type":"tts","state":"start"\|"sentence_start"\|"stop","session_id":...,"text"?:<句子>}` | 播报状态机：`start`=开始说话（固件进 Speaking）；`sentence_start`=每句字幕；`stop`=本轮结束（固件回 Listening） |

> 全双工（realtime）下：后端在每轮首帧音频前重发 `tts start`（对抗固件 3s 静音看门狗）；陈旧 `stop` 会被跳过以防打断新轮。固件应把 `stop` 当权威信号回到聆听。

### 2.5 二进制音频帧

🔴🔴 **【头号坑 · 上下行采样率不对称】先读这一段，全功能测试语音交互 80% 的问题出在这。**

| 方向 | 采样率 | 声道 | 帧长 | 每帧样本数 | 编码/解码 |
|---|---|---|---|---|---|
| **上行（mic → 后端）** | **16000 Hz** | 单声道 | 60ms | **960 样本** | 固件录音 PCM(16k) → libopus 编码 → 发二进制帧 |
| **下行（后端 TTS → 设备）** | **24000 Hz** | 单声道 | 60ms | **1440 样本** | 收二进制帧 → libopus 解码(**务必 24k**) → PCM → I2S 播放(24k) |

- **上行 16k 是后端 ASR 决定的，写死、不可协商**。固件麦克风 I2S 必须按 **16000Hz** 配置录音、编码。
- **下行 24k 由 GREET 的 `audio_params.sample_rate` 告知（=24000）**。固件播放 I2S 必须按 **24000Hz** 配置，**解码器也必须用 24000Hz 初始化**——用 16000 解 24k 的包会直接爆音/时钟错乱。
- **两套 I2S 参数，分开设置**：录音 I2S = 16000Hz，播放 I2S = 24000Hz。不要共用一个采样率。
- 两者**帧时长都是 60ms**，但因为采样率不同，**每帧样本数不同**（上行 960 / 下行 1440）。按样本数编码解码，不要假设上下行帧一样大。
- 下行直连 WS = **裸 opus（无头）**；仅走 `mqtt_gateway` 路径才带 16 字节头。
- 对讲的音频是另一套参数（见 §3）：16k / **20ms** / 裸 opus（50 帧/秒）。**对话链路 60ms ↔ 对讲 20ms 也不能混用**。

> 一句话给固件：`录音16000编码发，接收24000解码播`。GREET 里的 `audio_params` 描述的是**下行/TTS** 参数；上行 16k 不在 GREET 里、是后端 ASR 硬约束。

### 2.6 实时全双工 / 服务端 AEC

- 设备 `listen` 带 `mode:"realtime"` → 后端置 `conn.client_aec=True`（服务端 AEC 开启）。
- 固件在 TTS 播放期间把"实际播放的 PCM"独立 opus 编码，经 `audio_playback_ref` 文本帧上行（base64）。后端解码→重采样 16k→以服务端到达时刻存入 `aec_audio_cache` 作远端参考。
- **收到第一条 `audio_playback_ref` 即自动开启服务端 AEC**，不依赖 hello 协商（2026-08-03 修复）。
- 参考时钟统一用服务端 `int(time()*1000)`（固件同源 ts 时基不一致，已回退）。

---

## 3. 对讲模式（Intercom）

> 完整设备端契约见 **`docs/INTERCOM_DEVICE_INTEGRATION_CONTRACT.md`**（从运行代码核实）。要点摘录：

- **进入开关**：hello 带 `"session":"intercom"`。后端强制 `mic_ts_header=False` + `client_aec=False`（对讲 AEC 在设备侧）。
- **音频**：同一条 WS 的二进制帧，**裸 opus**（16k / 单声道 / **20ms / 帧**，50 帧/秒恒定），无任何头字节。后端对设备↔设备**零转码**原样透传。
- **开关指令**（后端下发，文本帧）：
  ```json
  {"type":"iot","commands":[{"name":"DeviceControl","method":"SetIntercom",
    "parameters":{"peer":"<peer_device_id>","action":"start|stop"}}]}
  ```
  固件需新增 `DeviceControl` thing + 注册 `SetIntercom` 方法；`start` 必须**幂等**，`stop` 必须**无条件执行**（看门狗也会发 stop）。
- **事件**（后端→设备，文本帧）：`{"type":"intercom","event":"established|peer_offline|peer_left|peer_busy","peer":...}`。
- 🔴 **单 WS 模型**：后端注册表按 `device_id` 单槽覆盖，第二条 WS 会顶掉第一条的下行能力。推荐**方案 A**：对讲时断开主 WS、重连带 `session:intercom`、结束再切回（grace 30s 内重连自动恢复并补发 start）。
- **时序参数**：grace `GRACE_SEC=30`、bootstrap 自举（重发 start 最多 2 次）、watchdog（`connected` 满 6s 后，设备静默 >5s 且手机仍在发 → 下发 stop）、`CALL_TTL=30`（振铃超时）。
- 🔴 **DTX 必须关闭**：对讲"零上行"被定义为链路死，静音也须持续发帧，否则 5s 被 watchdog 掐断。
- 联调首选 **loopback 自环**：`POST /api/v1/devices/<id>/intercom {"action":"start","peer":"loopback"}`。

---

## 4. IoT 设备能力描述符驱动（零代码扩展）

后端支持设备**自描述**能力，固件上报后后端自动把每个 method 注册成 LLM 可调用的工具，无需后端改代码：

1. 设备 hello 后通过 `iot` 帧上报 `descriptors`（ThingManager 描述每个 Thing 的 method 列表）。
2. 后端 `register_iot_tools` 把每个 method 注册成工具 `{thing}_{method}`（如 `media_next`、`lamp_set`）。
3. 设备实时上行 `states`（状态镜像）与 `schedule`（定时任务镜像），供小程序读取与对账。
4. 当用户/小程序触发某能力 → 后端构造并回注设备 WS：
   ```json
   {"type":"iot","commands":[{"name":"<Thing>","method":"<method>","parameters":{...}}]}
   ```
   > 此路径**不经** `_COMMAND_METHOD_ALLOW` 小程序 HTTP 白名单（白名单只管小程序 `/command` 入口）。

小程序侧直接下发 IoT 指令走 `POST /api/v1/devices/{id}/command`（见 §7），允许方法白名单：`SetTimerTask / CancelTimerTask / ListTimerTasks / SetMainLight / SetDevice / SetAmbientLight`（对讲请用 `/intercom` 专用端点）。

---

## 5. 实时事件 WS（`/api/v1/events`）

独立通道（HTTP 口，与设备 8000 完全隔离），用于小程序/面板订阅设备实时事件。

- **鉴权**：`Authorization: Bearer <JWT>` 或 `?token=<JWT>`（微信 WS 只能走 Query）。
- **首帧 hello**：`{"type":"hello","online":[{"device_id","connected_at","client_ip","last_active"}],"ts":...}`
- **下行事件**（JSON）：
  - `device.online` / `device.offline` / `device.active`（设备上下线/活跃）
  - `device.bound`（绑定成功）
  - `voicemail.new` / `voicemail.played`（留言）
  - `intercom.call`（对讲呼叫）
  - `proactive.said`（主动对话触发）
- **保活**：服务端空闲 25s 发 `{"type":"ping","ts":...}`；客户端可回 `{"type":"pong"}`。
- 单用户并发上限 5 条；未知设备事件节流刷新归属（5s）。

---

## 6. HTTP 管理 API（小程序用户态，需 JWT）

统一守卫 `api_guard`（缺 JWT → 401）。所有响应默认带 CORS 头（`Access-Control-Allow-Origin: *`）。错误统一体：`{"code":int,"message":str}`（部分模块用 `{"ok":bool,"error":str,"message"?:str}`）。

### 6.1 鉴权 `auth`
| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/v1/auth/login` | `{code, nickname?, avatar?, phone_code?}`（微信 `wx.login` 的 code） | `{token, user:{user_id,openid,nickname,avatar,phone}}` |
| GET | `/api/v1/auth/me` | — | 当前用户信息 |

### 6.2 设备 `devices`
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/devices/bind-token` | 小程序领一次性 `bind_token`（配网下发） |
| POST | `/api/v1/devices/bind` | ESP32 配网后上报绑定 `{device_id, bind_token}`（或裸 `Authorization` 头）；返回 `device_token` |
| POST | `/api/v1/devices/bind-by-code` | 小程序输 6 位绑定码认领 |
| POST | `/api/v1/devices/unbind` | 解绑（status→unbound，清 device_token） |
| GET | `/api/v1/devices` | 列出名下设备（含 today/weekly 用量 + online/last_active） |
| GET | `/api/v1/devices/{id}/usage?days=` | 近 N 天用量（默认 7，上限 365） |
| GET | `/api/v1/devices/{id}/memory?detail=` | 互动记忆安全摘要（不吐原始记忆） |
| DELETE | `/api/v1/devices/{id}` | 删除自己名下且已 unbound 的设备（级联删 agent-config） |
| POST | `/api/v1/devices/{id}/intercom` | 对讲控制 `{action:"start\|stop", peer}`（`loopback` 自环） |
| POST | `/api/v1/devices/{id}/command` | 下发 IoT 指令（白名单方法，见 §4） |
| GET | `/api/v1/devices/{id}/reminders` | 定时任务列表（优先设备镜像，回退后端副本） |

> `/bind` 必须携带小程序下发的一次性 `bind_token`（或已登录的小程序 JWT），校验通过后才签发 `device_token`；其余设备侧接口均以 `device_token` 鉴权。

### 6.3 设备智能体配置 `agent-config`
| 方法 | 路径 | 说明 / 字段 |
|---|---|---|
| GET | `/api/v1/agent-config` | 列出名下已配置设备（含生效值） |
| GET | `/api/v1/agent-config/{id}` | 单设备最终生效配置（设备覆盖 + 全局回落） |
| PUT | `/api/v1/agent-config/{id}` | PATCH 更新：`wakeup_words(≤100)` / `prompt(≤4000)` / `tts_provider` / `voice` / `speed(0.5–2.0)` / `volume(0–1)` / `tone` / `dialect` / `device_name(1–50)`；校验失败 422 |
| DELETE | `/api/v1/agent-config/{id}` | 删设备配置，回落全局 |

### 6.4 角色预设 `presets`
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/presets` | 内置人格目录（`id/name/description/emoji`），当前 8 套 |
| GET | `/api/v1/presets/{id}` | 单预设完整内容（含 config 人格包） |
| POST | `/api/v1/presets/{id}/apply?device_id=` | 一键套用（合并语义），返回套用后的生效配置；缺 device_id→400，预设不存在→404，越权→403 |

### 6.5 对讲编排 `intercom`
| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/v1/intercom/call` | `{my_device_id?, target_device_id, target_friend_id?}`（`my_device_id` 可省/为 `"phone"` → 手机端） | `{call_id, status:"calling\|connected"}`；冲突→409 |
| POST | `/api/v1/intercom/call/end` | `{call_id}` | `{call_id, status}` |
| GET | `/api/v1/intercom/call/active` | — | 当前活跃呼叫（`data:null` 表示无） |

### 6.6 离线语音留言 `voicemail`
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/voicemail` | multipart：`meta(json)` + `audio(binary)`；`meta.target_device_id` + `source_fmt`（默认 pcm）；时长 0.2–60s，≤2MB |
| GET | `/api/v1/voicemail/list?device_id=&status=&limit=&offset=` | 留言列表 |
| GET | `/api/v1/voicemail/{msg_id}/audio?device_id=` | 拉音频（`.fgvm`→wav 现转，小程序直接播） |
| POST | `/api/v1/voicemail/{msg_id}/read` | 标记已读 `{device_id}` |
| DELETE | `/api/v1/voicemail/{msg_id}?device_id=` | 删除 |

### 6.7 人格 `persona`
| 方法 | 路径 | 字段 |
|---|---|---|
| GET | `/api/v1/persona/{id}` | 读合并画像 + 文件层 |
| PUT | `/api/v1/persona/{id}` | 部分更新：`name / style / traits[] / catchphrase / taboo[]` |
| DELETE | `/api/v1/persona/{id}` | 清文件层，回落默认 |
| GET | `/api/v1/persona/{id}/schema` | 字段元数据（前端渲染表单） |

### 6.8 家庭知识 `family-knowledge`
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/family-knowledge/{id}` | 列出全部（`enabled=false` 时为空） |
| POST | `/api/v1/family-knowledge/{id}` | 新增/更新 `{content(≥2字), category?, tags?}`；同 content 去重 |
| DELETE | `/api/v1/family-knowledge/{id}?fact_id=` | 删一条 |
| DELETE | `/api/v1/family-knowledge/{id}/all` | 清空（需 body `{"confirm":"YES"}`） |
| GET | `/api/v1/family-knowledge/{id}/schema` | 类别元数据 |

### 6.9 主动对话 `proactive`
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/devices/{id}/proactive` | 读配置（开关/密度/静默时段/触发源） |
| PUT | `/api/v1/devices/{id}/proactive` | 更新配置 |
| POST | `/api/v1/devices/{id}/proactive/trigger` | 立即触发（可带 `reason`/`text`） |

### 6.10 视觉理解 `vision`
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/mcp/vision/explain` | multipart：问题 + 图片（≤5MB，支持 JPEG/PNG/GIF/BMP/TIFF/WEBP）；鉴权 `Authorization: Bearer <device_token>` + `Device-Id`/`Client-Id` 头；返回 `{"success":true,"action":"RESPONSE","response":<描述>}` |
| GET | `/mcp/vision/explain` | 健康检查（返回接口地址或配置提示） |

---

## 7. 设备侧 HTTP 接口（无小程序 JWT）

### 7.1 OTA（固件升级）
- **基础路径** `/fuguoai/ota`（POST 检查 / GET 信息），下载 `/fuguoai/ota/download/{filename}`。
- **POST** `/fuguoai/ota`：头 `device-id` + `client-id`（必填）；优先从头取 `device-model`/`device-version`，否则从 body（`board.type` / `application.version`）。
  返回：
  ```json
  {
    "name": "复果AI",
    "server_time": {"timestamp": <ms>, "timezone_offset": 480},
    "firmware": {"version": "<当前/新版本>", "url": "<下载地址或空>"},
    "websocket": {"url": "ws://<ip>:8000/fuguoai/v1/", "token": "<auth开启时>"}
      // 或 "mqtt": {...}（配置了 mqtt_gateway 时）
  }
  ```
  服务端在 `data/bin/` 按 `{model}_{version}.bin` 命名匹配，若有更高版本则下发 `firmware.url`。
- **GET** `/fuguoai/ota`：返回 `{name, websocket, ota:"http://<ip>:<http_port>/fuguoai/ota/"}`。
- **下载** `/fuguoai/ota/download/{filename}`：仅允许 `data/bin/*.bin`（防目录穿越）。

### 7.2 天气 `weather`
- **GET** `/api/v1/weather?location=<城市或空（按IP定位）>`，头 `device-id`（必填）。
- 返回：`{"ok":true,"data":{"city","icon","desc","temp","high","low","humidity","provider","ts"}}`。
- `icon` 为归一化枚举（wttr/qweather 间切换固件无需改动）。面向有屏/灯效的物理设备，**不走语音链路**。
- 失败：缺 device-id→400；上游异常→502。

---

## 8. 配置项与联调要点

- **`server.port`**（8000）：设备语音 WS；**`server.http_port`**（模板 8003，线上 8002）：HTTP 全部 REST/OTA/events/metrics。
- **`device_ws_auth.enabled`**：设备 WS 令牌校验开关（当前 `true`）。
- **设备侧 HTTP（OTA / weather）鉴权**：随绑定下发的设备凭证统一管控，不另设独立开关；详见 §1.1。
- **`enable_websocket_ping`**：设备 WS ping/pong 保活（默认未启用）。
- **`config["fuguoai"]` GREET 块**：`type/version/transport/audio_params/wakeup_words`，运行期注入 `session_id`。
- **`/metrics`**（8002，无鉴权，内网）：对讲 `fuguoai_intercom_relay_uplink/downlink/drop_total`、用量、连接数等，联调排障首选。

### 已知坑（固件侧务必留意）

> 🔴 **#0 头号坑 · 上下行采样率不对称（详见 §2.5）**：上行 mic = **16000Hz**（后端 ASR 硬约束，录音 I2S 配 16k）；下行 TTS = **24000Hz**（GREET `audio_params.sample_rate` 告知，播放 I2S + opus 解码器都须配 24k）。**两套 I2S 参数分开设**。两者都是 60ms 帧，但样本数不同（上行 960 / 下行 1440）。全功能语音测试出问题先查这一条。

1. **双 WS 单槽**：同 `device_id` 第二条 WS 顶掉第一条下行（对讲必须用单 WS 模式切换，见 §3）。
2. **帧长 60ms ≠ 20ms**：对话 60ms、对讲 20ms，混用直接 `transcode_fail`。
3. **麦流 8B ts 头 vs 对讲裸 opus**：`mic_ts_header=1` 才加头；对讲连接强制无头。
4. **对讲 DTX 必须关**：静音也持续发帧，否则被 watchdog 掐断。
5. **GREET 回显 audio_params**：不上报则退回 24000/60ms，与对讲冲突。
6. **abort 语义**：realtime 下仅 `reason:"barge_in"` 被忽略；POWER/按键（无 reason 或 `"button"`）必须停播转聆听。

---

## 9. 联调 / 上线 checklist

- [ ] 设备 WS 带 `device-id` 头 + `Bearer device_token` 建连，收到 GREET（`session_id` 非空）。
- [ ] 对话链路：发 `listen` start/stop + 麦流裸 opus → 收到 `stt` + `tts` + 下行 opus 音频。
- [ ] 对讲（如支持）：hello 带 `session:intercom` + `audio_params 16000/1/20`；loopback 自环能听到自己；`uplink_total>0` 且 `transcode_fail=0`；静音 10s 不断连。
- [ ] OTA：POST `/fuguoai/ota` 带 `device-id`+`client-id`，返回 `firmware.url`（若有新固件）。
- [ ] 天气（如需要）：GET `/api/v1/weather` 带 `device-id` 返回结构化 JSON。
- [ ] IoT（如支持）：上报 `descriptors` → 小程序/LLM 可调对应工具；`states`/`schedule` 镜像可读。
- [ ] 事件 WS：小程序 `wss://.../events?token=<JWT>` 收到 101 + `hello` 首帧。
- [ ] 管理 API：小程序 `login` 拿 JWT → 各 `/api/v1/*` 守卫 200。
- [ ] 排障先看 `/metrics`（8002）与后端日志 `[device_id][fw_version]`。

---

**附：关键代码位置索引（后端，便于对照）**

| 内容 | 位置 |
|---|---|
| 设备 WS 鉴权 | `fuguoai_core/websocket_server.py:_handle_auth` |
| 握手 / GREET / intercom 模式识别 | `fuguoai_core/handle/helloHandle.py` |
| 文本消息注册表（10 类型） | `fuguoai_core/handle/textMessageHandlerRegistry.py` |
| 下行 stt/tts + 裸 opus 发送 | `fuguoai_core/handle/sendAudioHandle.py` |
| 对讲契约（设备端） | `docs/INTERCOM_DEVICE_INTEGRATION_CONTRACT.md` |
| 事件 WS | `fuguoai_core/api/events_routes.py` + `docs/EVENTS_WS_COORDINATION.md` |
| 路由自动发现 | `fuguoai_core/api/registry.py`（`@register_routes` + `apply_routes`） |
| 配置装载（双配置陷阱） | `fuguoai_config/config_loader.py` |
