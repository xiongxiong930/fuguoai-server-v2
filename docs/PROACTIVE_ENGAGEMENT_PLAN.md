# 主动对话引擎（Proactive Engagement）方案

> 分支：`feature/proactive`（基于 `feature/tier1`，未部署）
> 状态：**方案阶段**，待评审。本文只定设计与接口，不含实现代码。
> 创新定位：把复果 AI 从「被动应答设备」升级为「主动陪伴伙伴」——体验跃迁级差异化，纯后端可立项。

---

## 1. 为什么这是"创新"而非"功能补全"

去年（及本段已完成的工作）做的是：**留言、对讲指标、联网提醒、设备面板**——全部是「用户触发 → 设备响应」的被动链路，或运营侧管理工具。

主动对话的本质不同：设备**自己决定什么时候、说什么、是否开启一轮对话**。这是智能设备的核心差异化维度，代码库探查确认该维度去年**全 0 命中**（`proactive` / `proactive_speak` = 0），属真空缺。

## 2. 与「联网提醒」的本质区别（避免重复造轮子）

| 维度 | 联网提醒（已实现） | 主动对话（本方案） |
|---|---|---|
| 触发 | 固定调度（once/daily/weekly/interval） | 情境 + 记忆驱动（时间/事件/空闲/记忆待跟进） |
| 内容 | 固定文本或一次性 TTS | **LLM 实时生成**的自然搭话 |
| 方向 | 单向播报（说完即止） | **双向对话起点**（期待用户接话） |
| 价值 | "别忘吃药" | "今天天气好，要不要听你昨天说的那首歌？" |

两者复用同一套下发通道（vm-server 推流），但**主动对话多了「内容生成」和「接话」两层**，是提醒的升维而非替代。

## 3. 核心架构

```
[触发器] --(评估)-->
[内容生成 LLM] --(自然开场白文本)-->
[TTS→opus帧] --(vm-server实钟推流)-->
[设备播放开场白] --(接话机制)-->
[实时对话管线 ASR→LLM→TTS]
```

### 3.1 触发源（proactive triggers）—— 已锁定的三类设计

> 2026-07-28 评审确定：暂按以下三类设计。

- **① 时间触发（复用联网提醒调度）**
  - 直接复用 `networked_reminder` 的 4 类调度（once/daily/weekly/interval）作为触发锚点。
  - 增强：某条提醒 fire 后，可顺带发起"相关搭话"（如提醒"该吃药了"→ 播完问"吃完感觉好点没？"），把单向播报升为对话起点。
  - 依赖：联网提醒功能（独立分支 `feature/networked-reminder`，合 main 后可用）。

- **② 记忆触发（从提醒/长期对话提取）**
  - **纪念日 / 重要日期**：从对话或提醒文本里抽取实体 + 日期（"妈妈的生日 8/15"、"结婚纪念日"）→ 到期主动提起。
  - **多频聊天时间**：统计历史对话时间戳，算出用户惯常活跃窗口（非固定提醒，而是行为模式）→ 在该窗口内轻量搭话。
  - **待跟进事项**：对话中"改天/下次/记得"等意图 → 抽为跟进任务，到点触发。
  - **⚠️ 先决依赖（见 §3.1.1）**：需新增轻量长期记忆 store + 离线抽取 pipeline，目前代码均为 0。

- **③ 主动外接触发（设备传感器）**
  - 红外 / 人体感应（PIR）/ 多模态（视觉）感知到场 → 触发"你回来啦 / 该休息了"类搭话。
  - **⚠️ 先决依赖（见 §3.1.2）**：需设备把 presence/传感器事件上报后端，目前**无此通道**（已核实仓库仅有在线/心跳/音频/对讲上报）。属跨团队依赖 + 后端需新增 ingestion 端点。隐私敏感，默认关闭、需用户授权。

#### 3.1.1 记忆提取先决（需立项子模块）
- 轻量长期记忆 store（跨会话、按 role_id/device_id 存事实/偏好/日期实体），与"长期记忆+人格"创新维度共用。
- 异步 LLM 抽取 pipeline：对话/提醒转写 → 实体（纪念日、偏好、待办、情绪）→ 写长期记忆。
- 多频聊天时间：从对话时间戳离线聚类出活跃窗口。

#### 3.1.2 外接触发先决（跨团队）
- 固件：新增 presence / IR / PIR / multimodal 事件上报（经既有设备 WS）。
- 后端：新增事件 ingestion 端点（或复用 `/api/v1/events` 上行），写入 presence 状态供引擎消费。
- 隐私：本地处理优先、用户显式开启、可审计。

### 3.6 补充触发维度建议（待评审采纳）

| 维度 | 说明 | 依赖 |
|---|---|---|
| **沉默回归** | 设备 N 天未被对话 → 轻量问候"好几天没聊了，最近好吗？" | 零依赖，纯后端，强烈建议加 |
| **上下文延续** | 上次"明天再跟你说那首歌" → 次日/到点主动续上（记忆触发的子类，单列） | 长期记忆 |
| **环境/世界事件** | 天气突变（暴雨/高温预警）、日历事件、智能家居事件（门铃/灯）→ 自然提及 | 外部数据源/ IoT 事件，天气可先做 |
| **情绪/状态感知** | 从最近 ASR/对话语气检测低落/焦虑 → 主动关心 | 隐私敏感，本地优先+默认关 |
| **家庭联动** | 多设备家庭：A 的事件触发对 B 的轻量提及（"爸爸刚到家了"） | 隐私边界 + 家庭关系模型 |
| **习惯养成** | 基于联网提醒的习惯（学英语）→ 到点鼓励/复盘 | 复用 reminder 数据 |
| **随机惊喜** | 活跃窗口内随机轻量搭话（非固定点），增强"活人感" | 零依赖 |
| **反馈学习** | 记录用户对每次主动触发的响应（接话/忽略），轻量 bandit 调频率与时机 | 离线统计，差异化亮点 |

### 3.7 跨触发源仲裁（工程建议）
- 多源可能同窗触发 → 引擎层加 `TriggerArbiter`：评分（相关性 × 时机 × 用户状态）→ 取最高 → 护栏过滤 → 下发。
- 避免 presence + 时间窗口重合导致重复打扰（去重合并）。

### 3.2 内容生成（LLM）
- 调用现有 LLM provider，prompt 注入：设备人格 + 用户记忆摘要 + 触发情境 → 生成**一句自然开场白**（非播报腔）。
- 护栏：拒绝生成敏感/负向/催促性内容；长度限制（≤ 2 句）。

### 3.3 下发通道（复用 voicemail_player 的 vm-server）
- 新增 `proactive_say(device_id, text)`：TTS→`decode_to_pcm`→`pcm_to_frames`→复用 `play_one` 内部实钟推流（20ms 绝对时刻表，吸取 60ms 事故教训）+ `send_to_device(VM_PEER, "start"/"stop")`。
- **固件零改动下发开场白**（与留言同链路）。

### 3.4 接话机制（⚠️ 唯一跨团队依赖，见第 8 节）
开场白播放结束后，设备需进入「可对话」状态让用户接话。此行为取决于固件在 `vm-server` 音频结束后的处理。

### 3.5 护栏（proactive guardrails）
- **空闲前置**：复用 `_device_idle`（在线 + 无活跃会话 + 无透传）。
- **频率上限**：每设备每天 ≤ N 次（默认 3），可配置。
- **静默时段**：用户可设勿扰窗口（如 22:00–08:00）。
- **无响应退场**：开场白后 T 秒无用户语音 → 安静结束，不重试。
- **去重**：同一话题 24h 内不重复主动发起。
- **隐私**：仅对在线且在场设备；不主动提及敏感记忆。

## 4. 可复用组件（已确认存在，零新增依赖）

| 组件 | 位置 | 用途 |
|---|---|---|
| `VoicemailPlayer.play_one` / `VM_PEER` | `voicemail_player.py` | vm-server 实钟推流通道 |
| `_device_idle` | `voicemail_player.py` | 空闲/在场检测 |
| `decode_to_pcm` / `pcm_to_frames` | `voicemail_store.py` | TTS→opus 帧 |
| `MemoryProvider.query_memory(role_id)` | `providers/memory` | 提取用户话题/偏好 |
| LLM provider（`OpenAILLM`） | `providers/llm` | 开场白生成 |
| 15s 轮询 worker 框架 | `networked_reminder_scheduler.py`（参考） | 触发器扫描骨架 |

## 5. 新增模块设计（实现阶段）

- `fuguoai_core/utils/proactive_engine.py`
  - `ProactiveEngine`：触发器注册 + 15s worker 扫描到点 → 内容生成 → `proactive_say` → 标 fired + 发 `proactive.said` 事件。
  - 护栏状态机（频率/静默/去重）。
- `fuguoai_core/api/proactive_routes.py`
  - `GET/PUT /api/v1/devices/{device_id}/proactive`（开关 + 密度 + 静默时段配置）
  - `POST /api/v1/devices/{device_id}/proactive/trigger`（立即触发，调试用）
  - `app.on_startup` 启动 worker；复用 `/api/v1/events` WS。
- `test_proactive_engine.py`：触发器评估/护栏/去重（离线，标准库）。

## 6. 接口草案

```http
PUT /api/v1/devices/{device_id}/proactive
Authorization: Bearer <user_token>
{ "enabled": true, "max_per_day": 3, "quiet_hours": ["22:00","08:00"], "topics": ["english","music"] }

POST /api/v1/devices/{device_id}/proactive/trigger
{ "reason": "test", "text": "optional override" }
```

事件：`proactive.said` `{device_id, text, trigger, fired_at}`（复用 event_bus）。

## 7. 实施分期

- **Phase A（纯后端，已落地 feature/proactive 分支）**：定时器(复用 reminder.fired) + 沉默回归 + 随机惊喜 三类触发 + 全量护栏 + 跨源仲裁 + 配置接口(GET/PUT /proactive + POST /proactive/trigger) + proactive.say 事件；16/16 离线测试通过。
  - 下发复用 voicemail/reminder 的 vm-server 推流（固件零改动）；`proactive_say` 复用 `_tts_to_opus`+`_stream_opus_to_device`。
  - 接话未就绪时：开场白后静默或提示"想聊就喊我"，不阻塞 MVP。
- **Phase B（需固件/对讲确认）**：接话机制打通后，开场白自然转入实时对话。

## 8. ⚠️ 接话机制——唯一需跨团队确认的点

**问题**：`vm-server` 开场白播放结束（`send_to_device(VM_PEER,"stop")`）后，设备是否自动开启 mic 进入对话？

- **选项 1（理想，零固件改动）**：固件在 vm-server 音频自然结束后自动进入对话/对讲模式 → 后端只需推开场白，用户直接接话。
- **选项 2（需固件小改）**：固件当前播放完静默（如留言场景），需新增"播放完转入对话"行为。
- **选项 3（后端显式握手）**：后端推完开场白后发起一次对话会话（类似 intercom call，peer=对话服务），设备 mic 上行进入实时管线。

> 此点不影响 Phase A 上线（开场白下发本身就是新体验）。Phase B 推进前需固件/对讲团队答复选项 1 是否可行。

## 9. 红线合规

- HTTP 只扩不删改（全新增 `/proactive` 路径）。
- 8000 WS 协议零改动（复用现有 vm-server 帧格式）。
- 实时推送走 event_bus（`proactive.said`）。
- 不碰 chat()/TTS 核心生成逻辑（只调用，不修改）。
- 下发零固件改动；接话部分按第 8 节确认后定。
- 容器部署等明天对讲联调闸过 + 合 main 后进行。

## 10. 待确认问题（转固件/对讲团队）

1. `vm-server` 音频播放结束后，设备 mic/对话行为是什么？（决定选项 1/2/3）
2. 接话若走 intercom call 握手，现有 `SetIntercom` 帧是否支持"对话服务"peer？
