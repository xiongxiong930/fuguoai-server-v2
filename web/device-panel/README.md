# 复果AI · 设备管理面板（静态 Web）

纯前端、无构建步骤的设备运营/管理控制台。调用既有后端 HTTP API（8002 端口）
与尽力而为的 events WS，自身不引入任何新后端状态。

## 功能
- **设备总览**：admin token → `GET /api/v1/admin/devices`（全部设备，含实时在线态）；
  用户 token → `GET /api/v1/devices`（名下设备）。表格展示在线/状态/归属/最近活跃，
  支持搜索、手动刷新、15s 自动刷新。
- **设备详情**：用量(`/usage`)、记忆(`/memory`)、留言(`/reminders`)走用户态接口；
  admin token 下这些标签页会 403（需设备归属 token），属预期。
- **动作**：下发 IoT 指令(`/command`)、触发对讲(`/intercom`)、解绑(`/unbind`)。
- **事件流**：尽力而为连接 `/api/v1/events` WS（带 `?token=` 兜底）；
  浏览器原生 WebSocket 不支持 Authorization 头，故 WS 鉴权可能失败，已自动退回轮询。

## 运行
1. 用任意静态服务器托管本目录，例如：
   ```bash
   cd web/device-panel
   python3 -m http.server 8080
   # 浏览器打开 http://localhost:8080
   ```
   或部署到 CloudStudio / 对象存储等静态托管。
2. 在顶部填写：
   - **API Base**：后端 HTTP 地址，例如 `https://<your-domain>`（不含 `/api/v1`）。
   - **Token**：admin token（运营总览）或某设备归属用户的 token（完整管理）。
3. 点「连接」即可。

## 红线合规
- 后端仅**新增** `GET /api/v1/admin/devices`（admin 守卫、CORS），不改任何既有路由。
- 不触碰 8000 WS 协议、不改动 chat/TTS 路径。
- 本面板为静态资源，可独立于 159 容器部署；后端接口在 `feature/device-panel` 分支，
  待对讲联调闸过 + 合 main 后随部署生效。

## 后端接口说明
| 方法 | 路径 | 守卫 | 说明 |
|------|------|------|------|
| GET | `/api/v1/admin/devices` | admin token | 全部设备总览（实时在线 + 状态 + 归属 + 最近活跃） |
