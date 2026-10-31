# 对讲通话质量指标 API 技术方案 v0.1（评审稿）

> 状态：设计文档，未立项未编码。目标：把 07-28 联调期的「一次性落盘定罪」诊断资产
> 产品化为**常驻轻量指标**，以后联调/线上排障一条 curl 看健康度，不再靠 flag+bin 落盘。
> 红线自查：HTTP 只扩 ✅；8000 WS 零改动 ✅；纯内存计数，不落盘不加锁争用 ✅。

## 1. 采集点（全部已存在于 relay 路径，只加计数不加逻辑）

在 `intercom_registry` 的会话对象上挂一个 per-call 统计结构（dict，无锁单写者）：

| 指标 | 采集位置 | 说明 |
|---|---|---|
| `up_frames` / `up_bytes` | relay 手机→设备发射点 | 60ms 事故的定罪指标；期望 50fps |
| `down_frames` / `down_bytes` | relay 设备→手机发射点 | 设备上行健康度；期望 ~50fps |
| `up_fps_10s` / `down_fps_10s` | 环形桶（6×10s 滑窗） | 逐 10s 帧率分布——正是钉死 14.6fps 的手段 |
| `encode_errs` / `decode_errs` | 既有 try/except 分支 | 只 += 1，不新增分支 |
| `rt_ratio` | 既有 `_timing_note` 口径（02b2649 修正后） | 实时率，≈1.0 为零堆积 |
| `last_up_ts` / `last_down_ts` | 每帧刷新 | 判「20s 硬截止」类断流：now-last>3s 即 stall |
| `started_at` / `mode` | 会话建立 | mode: device-device / phone-device(pcm|aac) |

内存策略：活跃会话实时更新；会话结束后快照保留**最近 20 通**（deque），进程重启即清零——
不做持久化（诊断型数据，持久化需求出现时再议）。

## 2. REST API（新增，管理口径走 admin token；用户口径走 api_guard 只见自己设备）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/intercom/stats` | 活跃会话实时指标数组（无则 `[]`） |
| GET | `/api/v1/intercom/stats/recent?limit=20` | 最近 N 通终态快照（含结束原因、时长、两向帧率分布） |

响应示例：
```json
{"ok":true,"data":[{
  "call_id":"call_xxx","mode":"phone-device/pcm","age_s":42,
  "up":{"frames":2100,"fps_10s":[50,50,49,50],"encode_errs":0,"stall":false},
  "down":{"frames":730,"fps_10s":[49,15,14,15],"decode_errs":0,"stall":false},
  "rt_ratio":1.01
}]}
```
↑ 这个示例就是 60ms 事故的形状——`down.fps_10s` 从 49 塌到 ~15，一眼定罪。

## 3. 告警（可选二期）

worker 每 10s 扫活跃会话：`fps_10s < 30`（应 50）或 `stall=true` → publish
`intercom.quality` 事件（event_bus → /api/v1/events），小程序/监控可订阅。MVP 不做，仅留字段。

## 4. 实施与门禁

- 改动面：`intercom_registry.py`（计数 ~40 行）+ `intercom_routes.py`（2 个 GET ~80 行）。
- ⚠️ 虽是只加计数，但落点在 P0 relay 热路径 → 必过 `regression_gate.py` 全量 +
  `test_intercom_transcode.py`，并加 `test_intercom_stats.py`（离线：计数正确性/滑窗/快照留存）。
- 部署走 `tools/deploy_file.sh`，与任何联调窗口错开。
- 量级：1 人日。建议排在留言 V-1 之前（更小、先见效，且留言真机验证时正好用它看播放帧率）。
