# 主动对话引擎 · 小程序对接文档（API + 事件）

> 分支：`feature/proactive`（Phase A 纯后端 MVP，已 push 到 origin）
> 状态：**未部署**，待明日对讲 A/B 联调闭环（v1.0.0）后随 `feature/tier1` 一起合 main 部署 159。
> 设计总纲见 `docs/PROACTIVE_ENGAGEMENT_PLAN.md`。

## 1. 这是什么

主动对话引擎让设备从「被动应答」变成「主动陪伴」：在合适的时机，由服务端冒充虚拟对端（vm-server）向设备推一段 TTS 开场白（如「好几天没和你聊天了，最近还顺利吗？」）。

- 下发通道：复用留言/联网提醒同一条 vm-server 虚拟对端，**固件零改动**。
- 实时推送：走既有 `/api/v1/events` WS（8002），新增 `proactive.said` 事件。
- 红线：HTTP 只扩不删改；8000 WS 协议零改；不碰 chat()/TTS 核心。

## 2. 接口清单

所有接口前缀：`/api/v1/devices/{device_id}/proactive`
鉴权：Bearer JWT（用户态 `api_guard`），且 `device_id` 须属于当前用户（否则 404）。

### 2.1 读取配置 — GET

```http
GET /api/v1/devices/{device_id}/proactive
Authorization: Bearer <user_jwt>
```

响应 200：

```json
{
  "ok": true,
  "data": {
    "enabled": false,
    "max_per_day": 3,
    "quiet_hours": ["22:00", "08:00"],
    "topics": [],
    "triggers": { "time": true, "silence": true, "random": true },
    "allow_external": false
  }
}
```

### 2.2 更新配置 — PUT

```http
PUT /api/v1/devices/{device_id}/proactive
Authorization: Bearer <user_jwt>
Content-Type: application/json
```

请求体（所有字段可选，缺省不改；只接受下列键）：

```json
{
  "enabled": true,
  "max_per_day": 3,
  "quiet_hours": ["22:00", "08:00"],
  "topics": [],
  "triggers": { "time": true, "silence": true, "random": true },
  "allow_external": false
}
```

字段含义：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| enabled | bool | false | **总开关，默认关闭**，需用户在设置页显式开启 |
| max_per_day | int | 3 | 每设备每天主动触发上限（最小 1） |
| quiet_hours | [str, str] | ["22:00","08:00"] | 勿扰窗口，支持跨午夜回绕（如 22:00–08:00） |
| topics | [str] | [] | 用户感兴趣的话题标签（Phase A 暂仅占位，用于未来记忆触发） |
| triggers | {time, silence, random} | 全 true | 三类触发源开关 |
| allow_external | bool | false | 外接触发（PIR/多模态）总授权，默认关（需固件上报通道，Phase A 未实现） |

响应 200：回显合并后的完整配置（`{ "ok": true, "data": {...} }`）。

### 2.3 立即触发（调试 / "现在跟我聊"）— POST

```http
POST /api/v1/devices/{device_id}/proactive/trigger
Authorization: Bearer <user_jwt>
Content-Type: application/json
```

请求体（可选）：

```json
{ "reason": "manual", "text": "要不要一起听首歌？" }
```

- 带 `text`：直接用该文本下发（绕过引擎评估）。
- 不带 `text`：走引擎评估取当前最优候选；若无候选则兜底「想和你聊会儿，最近怎么样？」。

响应 200：

```json
{ "ok": true, "fired": true, "trigger": "manual" }
```

`fired` = false 表示设备当前不可播放（离线 / 在对讲 / 在活跃呼叫中），不下发。

## 3. 实时事件 — proactive.said

经既有 `/api/v1/events` WS（8002）推送，小程序在事件分发里加一个 `case "proactive.said"` 即可。

事件体：

```json
{
  "type": "proactive.said",
  "device_id": "94:a9:90:1a:88:18",
  "text": "好几天没和你聊天了，最近还顺利吗？",
  "trigger": "silence_regression",
  "topic": "silence",
  "ts": 1753700000
}
```

`trigger` 取值：

- `reminder_related` —— 时间/提醒相关搭话
- `silence_regression` —— 沉默回归
- `random_surprise` —— 随机惊喜
- `manual` —— 手动触发

用途建议：小程序收到后可在设备卡片上显示「设备主动说了一句：…」，或作为「未读主动消息」红点；设备端会自动播放，无需小程序干预。

## 4. 触发逻辑与护栏（给前端的行为预期）

- **空闲前置**：仅在设备在线、且不在对讲态、不在活跃呼叫单时才下发。
- **频率上限**：每天不超过 `max_per_day` 次（默认 3）。
- **静默时段**：`quiet_hours` 窗口内不触发（跨午夜回绕已支持）。
- **24h 去重**：同一 `topic` 24 小时内不重复触发（避免「沉默回归」一天弹多次）。
- **仅在线**：设备离线则不触发。
- **扫描节奏**：后端每 15s 扫描一次；随机惊喜概率 `RANDOM_P = 0.01`/次（配合「今日未触发」门控，约等于每天至多 1 次随机）。
- **沉默回归阈值**：`DORMANCY_SEC = 3 天`（最近一次主动对话或用户活动距今 ≥ 3 天）。

⚠️ 前端不要假设「开了开关就一定立刻说话」——受上述护栏与扫描节奏约束。要立即验证，用 2.3 的 trigger 接口。

## 5. 小程序侧改动 checklist

1. **设置页**：新增「主动陪伴」开关 → `enabled`；可选「每天最多 N 次」→ `max_per_day`；可选「勿扰时段」→ `quiet_hours`；可选三类触发开关 → `triggers`。
2. **设备详情/对话页**：新增「现在聊会儿」按钮 → 调 2.3 trigger（可带/不带 text）。
3. **事件分发**：`/api/v1/events` WS 的 `onmessage` 增加 `case "proactive.said"`，在 UI 呈现（红点/气泡/历史）。
4. **状态展示**：读取配置（2.1）初始化开关状态，PUT 后回显。
5. **隐私说明**：`allow_external` 默认关，PIR/多模态外接尚未实现，UI 可先不暴露或置灰。

## 6. 最小对接示例（JS）

```js
const BASE = 'https://<your-domain>'; // 或 159:8002，按现有 BASE_URL 约定

// 读取配置
async function getProactive(deviceId, token) {
  const r = await fetch(`${BASE}/api/v1/devices/${deviceId}/proactive`, {
    headers: { Authorization: `Bearer ${token}` }
  });
  return (await r.json()).data;
}

// 更新配置（开启主动陪伴，每天最多 2 次，勿扰 22:00-08:00）
async function setProactive(deviceId, token, patch) {
  const r = await fetch(`${BASE}/api/v1/devices/${deviceId}/proactive`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify(patch)
  });
  return (await r.json()).data;
}

// 立即触发
async function pokeNow(deviceId, token, text) {
  const r = await fetch(`${BASE}/api/v1/devices/${deviceId}/proactive/trigger`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify(text ? { reason: 'manual', text } : {})
  });
  return (await r.json());
}

// 事件 WS（在现有 /api/v1/events 分发里加）
ws.addEventListener('message', (e) => {
  const ev = JSON.parse(e.data);
  if (ev.type === 'proactive.said') {
    console.log(`设备 ${ev.device_id} 主动说：${ev.text}（触发：${ev.trigger}）`);
    // TODO: 红点 / 气泡 / 历史记录
  }
});
```

## 7. Phase A 范围与未做项（给团队的调优/扩展清单）

已实现：

- 三类触发（时间/沉默回归/随机惊喜）+ 护栏 + 跨源仲裁 + vm-server 下发 + `proactive.said` 事件 + 配置接口。

**未做（代码量 0，是后续扩展点）**：

1. **记忆触发**：需轻量长期记忆 store + 离线 LLM 抽取 pipeline（纪念日/高频聊天时间）。
2. **外接触发（PIR/多模态）**：需固件侧 presence/PIR 事件上报 + 后端 ingestion 通道（跨团队，仓库暂无通道）。
3. **Phase B 接话**：开场白播放后设备「接话」机制取决于固件在 vm-server 音频结束后的行为，待固件 owner 答复。

调优建议（可直接改 `proactive_engine.py` 常量或经配置下发）：

- `DORMANCY_SEC`（沉默回归窗口）、`RANDOM_P`（随机概率）、`DEFAULT_MAX_PER_DAY`（频率）、`QUIET_DEFAULT`（勿扰）。

## 8. 部署状态

- 当前 **未部署 159**（等明日对讲 A/B 闭环 → v1.0.0 → 合 `feature/tier1` 与 `feature/proactive` → 部署）。
- 代码已在 `origin/feature/proactive`，团队可 `git fetch && git checkout -b feature/proactive origin/feature/proactive` 拉取调优。
- 离线测试 `test_proactive_engine.py` 16/16 PASS；`py_compile` 通过。
