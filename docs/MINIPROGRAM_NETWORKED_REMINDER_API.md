# 联网定时提醒 · 小程序对接文档

> 分支：`feature/networked-reminder`（基于 `feature/tier1`）
> 后端代码：`fuguoai_core/utils/networked_reminder_store.py` / `networked_reminder_scheduler.py` / `fuguoai_core/api/networked_reminder_routes.py`
> 状态：**未部署**，待评审 + 明天对讲联调闸过后再合 main 部署。

## 1. 这是什么（与离线闹钟的区别）

| 维度 | 离线闹钟（已有，`reminder_store.py`） | 联网提醒（本文档） |
|---|---|---|
| 定时器在哪 | 设备本地 | **服务端云端** |
| 谁触发播报 | 设备自己 | **服务端到点生成 TTS → 推流到设备** |
| 固件改动 | 设备原生 | **零改动**（复用留言播放的 vm-server 虚拟对端通道） |
| 数据真源 | 设备镜像 + 服务端副本 | 服务端 JSON（`data/networked_reminders.json`） |

联网提醒 = 小程序设一条「文字 + 调度」，服务端到点把这段文字念给设备听。适合「每天早上 8 点提醒吃药」「每 30 分钟喝水」这类场景。

## 2. 接口清单（全部新增，HTTP 只扩不删改）

前缀：`/api/v1/devices/{device_id}/reminders/networked`
鉴权：小程序 JWT（`Authorization: Bearer <token>`），且设备须归属当前用户（否则 404）。

### 2.1 创建提醒
`POST /api/v1/devices/{device_id}/reminders/networked`
```json
{ "text": "该吃药了", "schedule": { "type": "daily", "hour": 8, "minute": 0 } }
```
成功：
```json
{ "ok": true, "data": { "rid": "nr1a2b3c4d5e6", "next_fire_at": 1753603200, "schedule": {...}, "text": "该吃药了" } }
```
失败：`{ "ok": false, "error": "invalid", "message": "text 超过 200 字" }`（400）

### 2.2 列表
`GET /api/v1/devices/{device_id}/reminders/networked?include_disabled=0`
```json
{ "ok": true, "data": { "device_id": "AA:BB:...", "count": 1,
  "reminders": [ { "rid":"nr...", "text":"该吃药了", "schedule":{...}, "status":"pending",
                   "next_fire_at":1753603200, "last_fired_at":null, "fire_count":0,
                   "created_at":1753516800, "last_attempt_at":null, "last_error_at":null } ] } }
```

### 2.3 取消（删除）
`DELETE /api/v1/devices/{device_id}/reminders/networked/{rid}`
→ `{ "ok": true, "removed": true }`

### 2.4 立即触发（测试 / "现在提醒我"）
`POST /api/v1/devices/{device_id}/reminders/networked/{rid}/fire`
- 设备在线且空闲 → 立刻 TTS 播报 → `{ "ok": true, "fired": true, "rid": "nr..." }`
- 设备离线/忙 → `{ "ok": true, "fired": false, "rid": "nr..." }`（不报错，不改调度）

## 3. 调度类型 `schedule`

| type | 字段 | 说明 |
|---|---|---|
| `once` | `fire_at` (unix 秒) | 一次性；过期则下一个扫描周期（≤15s）触发 |
| `daily` | `hour`(0-23) `minute`(0-59) | 每天此时；已过则顺延次日 |
| `weekly` | `hour` `minute` `repeat`(0-6 或 mon..sun 列表) | 每周指定星期 |
| `interval` | `every_seconds`(≥60) | 每隔 N 秒（首触发 = 创建后 N 秒） |

示例：
```json
{ "type":"weekly", "hour":9, "minute":30, "repeat":["mon","wed","fri"] }
{ "type":"interval", "every_seconds":1800 }
```

## 4. 事件推送（复用既有 `/api/v1/events` WS）

提醒**成功播报**时服务端发布：
```json
{ "type": "reminder.fired", "device_id": "AA:BB:...", "rid": "nr...", "text": "该吃药了", "ts": 1753603200 }
```
小程序在 events 订阅里加一个 `case "reminder.fired"` 即可（不订阅也不影响功能）。

## 5. 小程序侧改动 Checklist

- [ ] **创建页**：文本输入（≤200 字）+ 调度选择（一次性/每天/每周/间隔），调用 2.1
- [ ] **提醒列表**：调用 2.2 展示；与离线闹钟列表（`GET .../reminders`）可合并展示
- [ ] **删除 / 启停**：调用 2.3（删除）；如需暂停，`set_enabled` 接口可后续补充
- [ ] **"现在提醒我"**：调用 2.4 做手动播报测试
- [ ] **events WS**：新增 `reminder.fired` case（用于已播报回执 / 红点）
- [ ] **配额提示**：单设备最多 50 条，超限服务端返回 `最多 50 条` 提示

## 6. 最小对接示例（伪代码）

```js
// 创建
await wx.request({ url: `/api/v1/devices/${did}/reminders/networked`,
  method: 'POST', header: { Authorization: `Bearer ${token}` },
  data: { text: '该吃药了', schedule: { type: 'daily', hour: 8, minute: 0 } } });

// 列表
const { data } = await wx.request({ url: `/api/v1/devices/${did}/reminders/networked` });

// events WS 分支
ws.onMessage(e => {
  const ev = JSON.parse(e.data);
  if (ev.type === 'reminder.fired') console.log('已播报:', ev.text, ev.rid);
});
```

## 7. 注意事项 / 边界

- 设备离线或正在对讲时，提醒**不会丢失**：到点未播报则保持 `pending`，下一扫描周期（≤15s）重试；TTS 故障冷却 5 分钟再试。
- `once` 过期 → 自动标 `fired`；`daily/weekly/interval` 播报后自动重算 `next_fire_at`。
- 服务端为唯一真源，重启不丢（`data/networked_reminders.json`）。
- 与离线闹钟**并存不冲突**：离线闹钟仍由设备本地响；联网提醒由服务端响。
