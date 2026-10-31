# 声纹（说话人识别）用户态接口 · 小程序对接文档

> 适用范围：小程序「家庭成员 / 声纹」页。后端新增用户态 REST，复用小程序 JWT 鉴权。
> 路径前缀：`/api/v1/voiceprint`

## 鉴权

所有接口必须带小程序登录后拿到的 JWT：

```
Authorization: Bearer <小程序 JWT>
```

未带 / 非法 / 过期 → `401 {"code":401,"message":"未认证或令牌无效"}`。
（WS 握手无法带自定义头时可用 `?token=<jwt>`，REST 请用 Authorization 头。）

## 音频契约（重要）

注册音频必须是 **Base64(WAV)**，参数固定为：

| 项 | 值 |
|----|----|
| 采样率 | 16000 Hz |
| 声道 | 单声道 (mono) |
| 位深 | 16 bit (s16le) |
| 格式 | WAV（VoiceFormat=1） |
| 时长 | 建议 1~8 秒清晰人声 |
| 体积 | ≤ 2 MB（base64 后） |

小程序 `RecorderManager` 设置 `sampleRate: 16000, numberOfChannels: 1, format: 'wav'`
即可直出，无需后端转码。

## 接口

### 1. 注册成员

```
POST /api/v1/voiceprint/enroll
Content-Type: application/json
Authorization: Bearer <jwt>

{ "name": "爸爸", "wav_base64": "<Base64(WAV 16k mono)>" }
```

响应（成功）：

```json
{ "ok": true, "data": { "voiceprint_id": "vp_xxx", "name": "爸爸" } }
```

响应（失败示例）：

```json
{ "ok": false, "error": "enroll_failed", "message": "注册调用失败: ..." }
```

错误码：`bad_request`(缺参) / `bad_audio`(解码失败) / `too_large`(>2MB) /
`not_configured`(后端未配声纹) / `enroll_failed`(腾讯云拒绝，如音频无人声)。

> 同名去重：再次注册同一 `name` 会覆盖旧登记（voiceprint_id 变化）。

### 2. 列出已注册成员

```
GET /api/v1/voiceprint
Authorization: Bearer <jwt>
```

响应：

```json
{ "ok": true, "data": [ { "voiceprint_id": "vp_xxx", "name": "爸爸", "created_at": "..." } ] }
```

### 3. 删除成员

```
DELETE /api/v1/voiceprint/{voiceprint_id}
Authorization: Bearer <jwt>
```

响应：`{ "ok": true }` ；不存在 → `404 { "ok": false, "error": "not_found" }`

## 推荐小程序交互流程

1. 家庭成员页展示「已注册列表」（`GET /api/v1/voiceprint`）。
2. 用户点「添加成员」→ 输入称呼（爸爸/妈妈/哥哥…，`name`）→ 点「开始录音」用
   `RecorderManager` 录 1~8s → 停止 → `wx.arrayBufferToBase64` →
   `POST /api/v1/voiceprint/enroll`。
3. 注册成功刷新列表；可长按某成员「删除」（`DELETE`）。

## 已知限制（v1）

- 当前声纹为**服务端单组**（一个 `group_id`），未做多家庭隔离；同一后端实例下
  所有小程序用户共享同一组成员列表。多租户隔离为后续迭代。
- 识别（谁在说话）在设备对话时后台自动跑 1:N，识别到的 `name` 会注入 LLM 提示词，
  使设备能以正确称呼应答；本接口只负责「注册/管理」成员。
