# 离线语音留言（Voicemail）技术方案 v0.1（评审稿）

> 状态：设计文档，未立项未编码。前置：对讲 M1 声学闭环 + v1.0.0 tag。
> 红线合规自查：①HTTP API 只扩不删改 ✅（全部新增路由）②设备 8000 WS 协议零改动 ✅（复用既有 SetIntercom + 对讲二进制帧格式）③实时推送走 /api/v1/events(WS) ✅（event_bus 旁路发布）④不触碰 chat()/_get_segment_text/TTS 路径 ✅（独立旁路模块）。

## 1. 背景与目标

现状：呼叫单 pending TTL=30s（`intercom_call_store.CALL_TTL`），超时 → `failed(peer_offline)`，
用户想说的话直接作废。目标：**打不通 ≠ 白说** —— 超时/离线后可留言，对端上线后收到并播放。

核心场景（MVP 只做第一条）：
- **P→D**：家长在小程序呼叫设备失败 → 留言 → 设备（孩子/老人端）上线后自动播报。
- D→P（二期）：设备端留言 → 小程序收听。
- D→D（M2 后）：设备互留言。

## 2. 总体架构（全旁路，新增 2 个模块）

```
小程序                     HTTP(8002)                      存储                    设备(8000 WS)
录音上传 ──POST /voicemail──▶ voicemail_routes ──▶ voicemail_store ──┐
                                   │                data/voicemail/   │ 上线触发
event_bus ◀── voicemail.new ───────┘                                 ▼
/api/v1/events(WS) ─▶ 小程序红点            vm_player(虚拟对端) ──SetIntercom start──▶ 设备进对讲态
                                            └─[4B seq][opus] 20ms 节奏下发──────────▶ 喇叭播放
                                                播完 SetIntercom stop（上行 mic 帧丢弃）
```

- `fuguoai_core/utils/voicemail_store.py`：留言 CRUD + 落盘 + 配额/TTL 清扫 + 上线投递 worker。
- `fuguoai_core/api/voicemail_routes.py`：REST（`@register_routes` 自动挂载，与 intercom_routes 同款守卫/归属校验/CORS）。

## 3. 录音链路：MVP 选「上传式」，不做实时流式

| 方案 | 说明 | 结论 |
|---|---|---|
| **U 上传式（选定）** | 小程序 RecorderManager 本地录（PCM/mp3）→ 一次 POST 上传 → 服务端 PyAV 转 16k/mono → 切 20ms opus 帧落盘 | 零实时链路、失败可重传、不占 relay；转码走**纯 PyAV**（经验铁律⑦，禁 ffmpeg 管道） |
| S 流式复用 relay | phone WS 实时 PCM 流进 recorder | 复杂度≈半个对讲，收益仅省一次上传延迟，**不做** |

约束：单条 ≤60s；上传原始格式支持 pcm/mp3/aac（PyAV 统一解）。

## 4. 存储格式

```
data/voicemail/<target_device_id>/<msg_id>.fgvm     # 音频
data/voicemail/<target_device_id>/index.jsonl       # 元数据（追加写，读时合并状态）
```

`.fgvm` 容器（自描述，播放端零猜测）：
```
[8B  magic "FGVM0001"]
[4B  LE sample_rate=16000][2B LE channels=1][2B LE frame_ms=20]
[4B  LE frame_count]
repeat frame_count × { [2B BE frame_len][opus payload] }     # 不存 seq，播放时现生成
```
元数据行：`{msg_id, from:"phone-<uid>"|device_id, to, uid, created_at, duration_ms, status:"new"|"played"|"read", source_fmt}`。

配额/TTL：每设备最多 **20 条**、保留 **7 天**、单条 ≤60s；超配额拒收（`quota_exceeded`），
清扫挂在 voicemail worker（每小时）+ 写入时兜底。

## 5. 设备端播放：「服务端虚拟对端」——固件零改动

复用对讲下行既有事实：设备收 `SetIntercom start` 进对讲态，协议层
（`websocket_protocol.cc` OnData kSessionIntercom）剥 `[4B seq]` 后解 opus 播放。

投递流程（目标设备上线时，挂 `notify_online` 同款钩子）：
1. 有未播留言且设备**空闲**（不在呼叫单、不在低层会话、不在对话中）才投递；否则延后重试。
2. 下发 `SetIntercom {peer:"vm-server", action:"start"}`（帧结构与 `_set_intercom_cmd` 完全同构）。
3. 可选前导语（把 TTS 合成的「你有一条来自XX的留言」也切成 opus 帧走同通道，**不走对话 TTS 路径**）。
4. 按 **20ms 实钟节奏**（asyncio 定时，禁 sleep 累积漂移——参照对讲 60ms 实验教训，帧节奏是固件命门）逐帧发 `[4B seq][opus]`。
5. 发完等 500ms 排空 → `SetIntercom stop`；期间设备 mic 上行帧**直接丢弃**。
6. 标记 `status=played`，publish `voicemail.played` 事件。

⚠️ 风险与验证点（需一次真机验证，安排在对讲闭环后）：
- R1 设备在「对端永远不说话、单向收流」模式下的 AEC/状态机行为 → 板 A 实测。
- R2 播放中来真实呼叫的抢占：规则=真呼叫优先，立即 stop 留言、留言回 `new` 重投。
- R3 与 12s 无音频兜底拆除的关系：留言流是持续下行，不会触发；仍需实测确认。

## 6. REST API（全部新增，前缀 /api/v1/voicemail，api_guard + 归属校验）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/voicemail` | 留言上传。multipart：`meta`(json: target_device_id, source_fmt, duration_ms) + `audio`(binary ≤2MB)。→ `{msg_id}`；409 `quota_exceeded` |
| GET | `/api/v1/voicemail/list?device_id=&status=` | 留言列表（分页 limit/offset，默认 20） |
| GET | `/api/v1/voicemail/{msg_id}/audio` | 拉取音频（服务端 .fgvm→wav 现转，小程序直接播） |
| POST | `/api/v1/voicemail/{msg_id}/read` | 标记已读 |
| DELETE | `/api/v1/voicemail/{msg_id}` | 删除（本人留言或目标设备归属人） |

事件（event_bus → /api/v1/events WS，B-4 同构）：
```json
{"type":"voicemail.new",    "msg_id":"vm_xxx", "device_id":"<target>", "from":"phone-<uid>", "duration_ms":8300, "ts":...}
{"type":"voicemail.played", "msg_id":"vm_xxx", "device_id":"<target>", "ts":...}
```

小程序改动 = 「呼叫失败弹留言按钮 + 留言列表页」，**属新功能，按红线 7 需你方确认后才动小程序**。

## 7. 与呼叫状态机的挂接（intercom_call_store 只加不改）

- `_finish(..., "failed", "peer_offline")` 后追加 publish 一个提示字段：
  `intercom.call` 事件加 `"can_leave_message": true`（只增字段，老客户端无感）。
- 不引入新呼叫状态；留言与呼叫单解耦（留言不占 `_by_device` 占线表）。

## 8. 测试与门禁

- 新增 `test_voicemail_store.py`（离线）：fgvm 封装/解析回环、配额、TTL 清扫、状态迁移 → 纳入 `tools/regression_gate.py` L3。
- 播放节奏契约：复用 `test_intercom_transcode.py` 的帧断言风格，锁 20ms/[4B seq][opus]。
- 真机验收：R1-R3 三个风险点 + 端到端（呼叫失败→留言→设备上线自动播→小程序状态流转）。

## 9. 工作量与里程碑

| 步骤 | 内容 | 量级 |
|---|---|---|
| V-1 | voicemail_store（fgvm+配额+清扫）+ 离线测试 | 1.5d |
| V-2 | voicemail_routes（5 接口 + 事件） | 1d |
| V-3 | vm_player 虚拟对端播放 + R1-R3 真机验证 | 1.5d |
| V-4 | 小程序录音/列表页（**需确认**） | 你方评估 |

约 4 人日服务端量级，预计新增原创代码 ~1200 行（同时稀释相似度，与 Phase 5 互补）。
