# 设备管理 · 前后端协调说明（给小程序 / 管理后台侧）

> 目的：复果AI 后端已把「设备管理」所需的全部能力通过 HTTP API 暴露。**小程序侧的「管理后台」是生产 UI 的归属方**；
> 本仓库附带的 `web/device-panel/` 仅作为**参考实现 / 调试工具**，不应视为要替换你们现有管理后台的独立产品。
> 本文档用于前后端对齐接口面与分工，便于管理后台直接消费后端 API。

## 1. 分工边界

| 层 | 归属 | 说明 |
|----|------|------|
| 后端 API（设备数据 / 动作 / 实时态） | 复果AI server（`feature/device-panel` 分支，待合 main 后部署） | 提供数据读写与下发能力 |
| 生产管理后台 UI | 小程序 / 管理后台团队 | 你们现有的管理后台调用下方 API 即可 |
| `web/device-panel/` 静态面板 | 复果AI server（参考） | 纯前端 demo，验证接口可用性；可忽略或用作联调参考 |

> 后端**只扩不删改**：本次新增的仅是 `GET /api/v1/admin/devices` 一个 admin 总览接口，
> 其余均为既有用户态接口，小程序侧此前已在用，无破坏性变更。

## 2. 设备管理能力 API 总览

Base：`https://<host>/api/v1`（HTTP 8002）。鉴权见 §3。

### 2.1 运营总览（新增，admin only）
```
GET /api/v1/admin/devices
```
- 守卫：`Authorization: Bearer <FUGUOAI_ADMIN_TOKEN>`（服务端环境变量注入，不进代码/镜像）。
- 响应：`{ "ok": true, "count": N, "data": [ { device_id, user_id, device_name, status,
  online(实时心跳态), online_stored(落库态), last_seen, last_seen_rel, activated_at }, ... ] }`
- 排序：在线优先，其次最近活跃倒序。
- CORS：`Access-Control-Allow-Origin: *`（管理后台跨域可直接调用）。
- **用途**：运营侧看「全部用户的所有设备」在线/状态/归属，替代逐用户拉取。

### 2.2 用户态设备列表（既有，小程序已在用）
```
GET /api/v1/devices
```
- 守卫：小程序 JWT（`api_guard`）。
- 响应：`{ "ok": true, "data": [ { device_id, device_name, status, activated_at,
  online, connected_at, client_ip, last_active, today{sessions,turns,response_chars,duration_seconds},
  weekly{sessions,turns,response_chars,duration_seconds,last_active_ts} }, ... ] }`

### 2.3 单设备详情（既有，用户态）
```
GET /api/v1/devices/{device_id}/usage?days=7     # 用量明细（上限365）
GET /api/v1/devices/{device_id}/memory?detail=    # 互动记忆摘要
GET /api/v1/devices/{device_id}/reminders         # 离线闹钟列表
```

### 2.4 动作（既有）
```
POST /api/v1/devices/{device_id}/command          # IoT 指令
     Body: {"name":"DeviceControl","method":"<M>","parameters":{...}}
     M ∈ {SetTimerTask, CancelTimerTask, ListTimerTasks, SetMainLight, SetDevice, SetAmbientLight}
POST /api/v1/devices/{device_id}/intercom          # 对讲
     Body: {"action":"start|stop","peer":"<device_id>|loopback"}
POST /api/v1/devices/unbind                         # 解绑  Body: {"device_id":"..."}
DELETE /api/v1/devices/{device_id}                 # 删除设备
```

## 3. 鉴权

| 场景 | 方式 |
|------|------|
| 运营总览（admin/devices） | `Authorization: Bearer $FUGUOAI_ADMIN_TOKEN` |
| 用户态列表/详情/动作 | 小程序 JWT，经 `api_guard`（与现有小程序一致） |
| 事件 WS `/api/v1/events` | `api_guard` 读取 `Authorization` 头 |

> ⚠️ **事件 WS 浏览器限制**：原生 `WebSocket` 不能自定义请求头，无法携带 `Authorization`。
> 若管理后台是 Web 页且需要实时事件，建议：①用轮询（如 15s 拉一次总览）兜底；
> ②若网关支持，可在 WS 握手 URL 带 `?token=<JWT>`（需后端确认放行，本版未改 guards）。
> 当前 `web/device-panel` 已按此策略「尽力而为 + 失败退回轮询」。

## 4. 给管理后台的对接清单

- [ ] 运营总览页：调用 `GET /api/v1/admin/devices`（admin token），渲染表格（在线徽章/状态/归属/最近活跃）。
- [ ] 用户设备列表：沿用现有 `GET /api/v1/devices`（小程序 JWT），字段未变。
- [ ] 设备详情：沿用 `/usage`、`/memory`、`/reminders`（用户态）。
- [ ] 远程操作：指令/对讲/解绑 沿用 §2.4（用户态）。
- [ ] 实时性：运营总览用轮询（15s）即可；勿依赖浏览器 WS 头鉴权。
- [ ] CORS：admin 接口已开 `*`，用户态接口由 `devices_routes` 既有 `_add_cors` 处理；若管理后台独立域名，确认后端已放行（必要时在网关层补 `Access-Control-Allow-Origin`）。

## 5. 部署状态

- 后端代码在 `feature/device-panel` 分支，**尚未部署**（容器安全）。
- 上线流程：明天对讲联调闸过 → 打 `v1.0.0` → 合 `feature/tier1` + `feature/networked-reminder` + `feature/device-panel` → 部署 159。
- 管理后台团队可在闸过后，用预发/生产地址联调 `GET /api/v1/admin/devices`。

## 6. 参考实现

`web/device-panel/`（index.html + app.js + styles.css + README.md）是上述接口的纯前端 demo，
可直接 `python3 -m http.server` 起本地预览，用于联调验证；生产 UI 以小程序管理后台为准。
