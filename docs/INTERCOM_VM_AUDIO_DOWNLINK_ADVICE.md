# 对讲下行音频通道实现建议（适配 vm-server 虚拟对端）

> 致：对讲固件团队（v1.6.0）  
> 发件：复果 AI 后端 / 小程序团队  
> 日期：2026-08-13  
> 主题：留言、联网提醒、主动播报等功能依赖 `SetIntercom` 下行音频通道，请在固件侧补齐播放处理。

---

## 1. 背景与现象

小程序新增功能：

- **离线语音留言**：用户给设备留语音，设备上线后自动播报。
- **联网提醒**：小程序设「每天 12:45 喝水」等提醒，到点由后端 TTS 推流到设备播报。
- **主动播报 / speak_request**：后端主动给设备发文本，设备 TTS 播报。

这些功能在后端已跑通（推流帧成功发送到设备 WS 连接），但在部分设备上表现为：

- 小程序端显示「已播报」；
- 设备端**没有声音**。

经后端排查，这些功能**全部复用对讲（Intercom）下行音频通道**，即服务端冒充一个虚拟对端 `vm-server`，通过 `SetIntercom` 指令让设备进对讲态，再逐帧下发 opus 音频。

**结论**：若设备固件没有实现对讲下行音频播放（或不识别 `vm-server` 对端），这些功能必然静默。

---

## 2. 后端推流协议（请固件按此实现）

### 2.1 控制指令：SetIntercom

后端下发 JSON 消息（与真实对讲完全同构）：

```json
{
  "type": "iot",
  "commands": [{
    "name": "DeviceControl",
    "method": "SetIntercom",
    "parameters": {
      "peer": "vm-server",
      "action": "start"
    }
  }]
}
```

播完下发：

```json
{
  "type": "iot",
  "commands": [{
    "name": "DeviceControl",
    "method": "SetIntercom",
    "parameters": {
      "peer": "vm-server",
      "action": "stop"
    }
  }]
}
```

**注意**：`peer` 可能是 `vm-server`，也可能是真实对讲对端 `phone-<user_id>`。两者在音频下行通路上应**等价处理**，不要只对 `phone-*` 放音、把 `vm-server` 静音或忽略。

### 2.2 音频帧格式

`SetIntercom start` 后，后端通过设备 WS 连接发送**二进制帧**（不是 JSON）：

```
[4B 大端序 seq][N B opus payload]
```

- `seq`：从 0 开始递增，32-bit 无符号大端序，可溢出循环；
- `opus payload`：单个 opus 帧，单声道，16kHz，20ms（对应 320 个采样点）；
- 发送节奏：每 20ms 一帧，后端按绝对实钟发送，不累积漂移；
- 播放前：设备应剥掉 4B seq 头，把剩余 opus payload 解码为 16kHz/mono/s16le PCM 后从喇叭输出。

### 2.3 与真实对讲的等价性

真实对讲时，手机端也是以同样格式下发 `[4B seq][opus]` 帧。因此设备端若已有对讲下行播放能力，**只需把 `peer=vm-server` 同等对待**，不需要新增协议。

---

## 3. 固件需要做的事

### 3.1 基础要求（必须）

收到 `SetIntercom start`（任意 peer）后：

1. 进入对讲播放态；
2. 准备 opus 解码器（16kHz / mono / 20ms）；
3. 收到二进制 `[4B seq][opus]` 帧时：剥 seq，opus 解码，PCM 送喇叭；
4. 收到 `SetIntercom stop` 后退出播放态，释放资源。

### 3.2 关键易错点

- **不要把 `vm-server` 当无效对端**：部分设备可能只认 `phone-*` 格式的真实用户，遇到 `vm-server` 直接丢弃。请把 `vm-server` 视为合法对端。
- **对讲通道已通，但 vm-server 没声音**：请检查是否在下发帧处理里按 `peer` 做了特判（例如只对 `phone-*` 写 I2S）。
- **没有对讲功能**：若设备本身不支持对讲下行播放，则留言/提醒/主动播报全部无法出声，需要整体补齐对讲音频下行能力。

---

## 4. 验证方法

### 4.1 真机验证（推荐）

1. 小程序给在线设备留一条语音留言，或设一条联网提醒并点「立即触发」；
2. 同时抓取设备端 DIAG 日志；
3. 预期看到：
   - 收到 `SetIntercom start peer=vm-server`；
   - 持续收到二进制下行帧（长度几十到几百字节，取决于 opus 编码）；
   - 收到 `SetIntercom stop`。
4. 如果日志里有上述帧但设备无声 → 喇叭/I2S 播放路径未处理 vm-server 对端。

### 4.2 后端冒烟验证

后端已提供 159 环境。可用真实账号 + 设备 ID 调接口：

```bash
# 上传一条 6s 左右的 PCM 留言（需 JWT token）
POST https://<your-domain>/api/v1/voicemail
Content-Type: multipart/form-data
formData:
  meta: {"target_device_id":"<did>","source_fmt":"pcm"}
  audio: <16k/mono/s16le PCM 文件>
```

上传成功后设备在线即会自动投递。后端日志会显示：

```
[voicemail] 播放完成 vm_xxx -> <did> frames=...
```

若后端日志显示播放完成但设备无声，即可确认是固件侧未放音。

---

## 5. 边界说明

- 本建议只涉及**设备端对讲下行音频播放**这一环；后端推流链路、小程序 UI 已自证可用。
- 本机固件（fuguoai-ble）与对讲固件（v1.6.0）为不同团队维护；本文件面向对讲固件团队，本机固件不在本文档范围内。
- 若对讲固件需要后端调整帧格式/节奏/协议字段，请向后端团队提需求，不要私自改后端。

---

## 6. 参考代码位置（后端）

- 留言播放：`fuguoai_core/utils/voicemail_player.py`
- 联网提醒 / speak_request：`fuguoai_core/utils/networked_reminder_scheduler.py`
- SetIntercom 指令构造：见上 §2.1
- 下行帧发送：`struct.pack(">I", seq) + opus_payload`，20ms/帧
