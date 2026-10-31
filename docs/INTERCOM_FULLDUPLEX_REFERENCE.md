# 对讲全双工：固件 + 后端联合定位参考（含全网同类案例）

> 制定：2026-07-28 23:2x。用途：把"全双工最后一定落在固件 AEC/播放管线 + 后端 relay 节奏"两端协同的已知结论，与全网同类案例对齐，形成可执行的联合定位 playbook。
> 前置已坐实（见 INTERCOM_AB_TEST_PLAN.md + 16:57/17:45 落盘铁证）：**网关侧 100% 洗清**——uplink 2998 帧全合法发出、downlink 发手机 640B 裸 PCM 无头、decode err=0。真凶在固件侧。
> 发布状态：本参考**不阻塞 4 功能发版**（见 INTERCOM_AB_TEST_PLAN.md 顶部「发布解耦声明」）。对讲声学 track 是固件团队并行事项。

---

## 0. 后端已锁定的不变式（勿再动）

| 项 | 值 | 依据 |
|----|----|----|
| relay 发设备帧格式 | **裸 opus（4B seq 已移除，2026-08-02）** | 固件 2026-08-01 切裸 opus，relay 不再拼/剥 seq；设备↔设备 M2 透传仍含 4B seq（由固件处理，relay 原样透传） |
| relay 发手机 | 640B 裸 PCM 无头 | 手机解码逻辑已验证 |
| 编码节奏 | 逐 640B / **20ms** | f2096b8 已 revert 60ms，合 main |
| vm-server 虚拟对端推流 | 20ms 实钟节奏 | 留言/提醒/主动对话复用 |
| 网关堆积 | 零（无 asyncio.Queue，逐帧 send） | 延迟/堆积 100% 在固件侧 |

⚠️ **60ms 聚合已被实证为有害**（设备音频环 3× 减速 + 73s 尾巴），后端**绝不再聚合 60ms**。若固件要求更大帧降 WS 包率，默认仍 20ms，必须改则走"独立发送任务 + 缓冲"而非同步大帧。

---

## 1. 三个全网同类案例（锚点）

### 1.1 esphome-intercom —— ESP32-S3 + 单 I2S 总线 ES8311 单麦全双工（最强镜像）
- GitHub: `AngeloDL90/esphome-intercom`（原 `n-IA-hane/intercom-api`、`jrote1/intercom-api`）。
- **单总线全双工本身难**：标准 ESPHome `i2s_audio` 无法在同一 I2S 总线同时驱动 mic+speaker；`i2s_audio_duplex` 组件解决（true full-duplex on single I2S bus）。
- **AEC 参考信号是"单一最影响配置选择"**：三种模式——①stereo digital feedback（ES8311 DAC loopback，最优）②TDM hardware slot reference ③ring buffer direct TX（单总线无延时调谐）。**无参考 AEC 必失败**（mic 回收喇叭声）。
- 作者踩坑历程：UDP 包时序、I2S 全双工、jitter buffer、AEC 收敛、**reference buffer alignment（参考缓冲对齐）**、wake word 误触发、slot_bit_width。
- 作者自述"深度熟悉 ESP32 看门狗定时器、heap_caps_calloc" → 印证全双工音频任务的 WDT 风险。
- 总线跑 48kHz（codec 原生）+ FIR 31-tap 抽取到 16kHz 处理；intercom 走 16kHz over TCP。

### 1.2 ESP32 语音方案稳定性（看门狗 / WiFi-BLE 共存 / 缓冲）
- 故障 A（周期重启）= `Task watchdog got triggered`，任务名通常为 `audio_task` → 未喂狗。根因：网络任务阻塞音频任务；**WiFi 弱信号反复重连占 CPU 数百 ms > 默认 WDT(~5s)**。
- 故障 B（延迟/丢帧）= I2S 时钟抖动 >3μs；电源纹波致**每 30s 一次语音截断**。
- 硬件级对策：Opus 任务**绑 Core1**（App CPU，Core0 跑 WiFi 栈）；WDT 超时 ≥800ms（实测帧处理时长）；用 PSRAM 模组；I2S 走线远离天线 ≥15mm；0.1μF 去耦。
- 双核修正：音频解码优先级 ≥ WiFi 任务 + 2。

### 1.3 I2S 无声 + 看门狗（信号振铃 + 缓冲溢出）
- 双倍立体声数据喂单声道 I2S 驱动 → **内部缓冲溢出 / DMA 错误 → 触发看门狗重启**。
- 修复（立体声→单声道提左声道）后"流程跑完但没声音" → 信号完整性（振铃 ringing）物理层问题（示波器探针接触 DIN 才出声）。
- ⇒ 印证：**帧格式/声道数错配 → 缓冲溢出 → WDT → 设备重启/断流**，与 B2 20s 硬截止高度同构。

---

## 2. 症状 ↔ 案例 ↔ 根因假设 映射

| 我们的症状 | 最可能根因（基于案例） | 印证线索 |
|-----------|----------------------|---------|
| **B1 设备不出声 / 杂音** | ①AEC 参考未接或错配（播放管线）②帧时长错配（60ms 已 revert，20ms 待真听验）③I2S 声道/格式错配导致无声 | 1.1 AEC 参考=最影响项；1.3 声道错配无声 |
| **B2 上行 20s 硬截止** | **任务看门狗复位**（audio_task 阻塞未喂狗）/ 缓冲溢出·DMA 错→WDT / in_intercom_ 翻转 / WS 断链 | 1.2 故障A（WDT）；1.3 缓冲溢出→WDT；与 20s 窗口同构 |
| **双端杂音（有人声被淹没）** | AEC 参考缺失·未对齐 → mic 回收喇叭声（回声/噪声）；高增益 + 无参考 | 1.1 无参考 AEC 必失败；1.2 增益/噪声 |

> B2 注：固件无 20s 定时器（超时 120s/心跳 8s），故 20s 截断非业务定时器，更像 **WDT 复位后的上行断链窗口** 或 **音频任务阻塞累积至 WDT 触发**（~5s 默认，20s 可能是自定义 WDT 或"多次小阻塞累积 + 重连"窗口）。

---

## 3. 给固件团队的联合定位探针清单（按症状，最高优先 B2）

> 若设备为 ESP32 / 类 FreeRTOS MCU，§1.2/§1.3 的 WDT / 双核结论直接适用；其余 MCU 取其架构思路（音频任务解阻塞、AEC 参考、帧格式）。

### B2 —— 上行 20s 硬截止（最可能 WDT，最高优先）
1. **探针① 开 WDT panic + 详细栈**：`CONFIG_ESP_TASK_WDT_PANIC=y` + `CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK=y` + `CONFIG_LOG_DEFAULT_LEVEL_DEBUG=y`。复现 20s 截断后抓串口 `task_wdt: Task watchdog got triggered` + 任务名 + 栈。
2. **探针② WS send 是否同步阻塞 audio_task**（机制③：大帧→send/recv 锁竞争；即便回退 20ms，若 send 同步且偶发阻塞 >WDT 仍触发）。改法：WS send 移出 audio_task（环形缓冲 + 独立网络任务），audio_task 只喂狗 + 产帧。
3. **探针③ 绑核 + 优先级**：audio/Opus 任务 `xTaskCreatePinnedToCore(..., 1)`（Core1/App CPU），优先级 ≥ WiFi + 2；`xPortGetCoreID()` / `uxTaskPriorityGet()` 验证。
4. **探针④ I2S 缓冲水位 + 声道数**：是否 stereo 数据喂了 mono 驱动导致溢出（§1.3）。
5. **判定**：截断瞬间有 `WDT_RST` 重启日志 → 坐实 WDT；`in_intercom_` 翻转 → 状态机；`websocket_==nullptr` → 发送断链。

### B1 —— 不出声 / 杂音（AEC 参考 + 帧格式）
1. **探针① AEC 参考源是否接入**（§1.1 三种之一）。单总线 ES8311 用 **stereo digital feedback（DAC loopback）** 最优；若无则用 **ring buffer direct TX**（播放即参考，无延时调谐）。
2. **探针② AEC 参考与 mic 捕获的时延对齐**（reference buffer alignment）——错位 → AEC 失效 → 噪声。
3. **探针③ 播放帧严格 20ms/640B**（已 revert 60ms）；确认 I2S 输出声道数 = 单声道（避 §1.3 无声）。

### 噪声（双端）
- 同 B1 探针①②；加：增益是否过高削波（§1.2 增益/噪声）；double-talk 时 AEC 非线性处理是否把人声当回声削掉。

---

## 4. 后端可配合的改动（仅固件确认需要 + 走回归门禁 L1/L2/L3）

- 若固件要求更大帧降 WS 包率：可协商但**默认 20ms**；必须 >20ms 则用"独立发送任务 + 缓冲"，**绝不回 60ms 聚合**（§1 已证有害）。
- 若固件要求网关发"纯 opus 无 4B 头"绕开剥头依赖：评估破坏设备↔设备 M2 透传契约风险（需先确认 M2 设备也剥头），谨慎。
- 保持不变：640B 裸 PCM 无头发手机、零堆积 relay、vm-server 20ms 实钟推流。

---

## 5. 联合 GO / NO-GO（按症状独立判定，不阻塞 4 功能发版）

| 症状 | 闭环判据 |
|------|---------|
| B2 | 抓到 WDT 日志 + 改 audio_task 解阻塞（移 WS send / 绑 Core1 / 喂狗）→ 复测 20s 内无截断 |
| B1 | AEC 参考接入 + 对齐 + 20ms 帧 + 单声道 → 真听干净人声 |
| 噪声 | AEC 参考修复后双端干净 |

任一未闭环 ≠ 阻塞功能发布（解耦声明）。

---

## 6. 参考链接
- esphome-intercom（GitHub `AngeloDL90/esphome-intercom`，原 `n-IA-hane/intercom-api` / `jrote1/intercom-api`）：ESP32-S3 单总线全双工 + AEC + jitter buffer 完整参考实现。
- ESP32 任务看门狗排障（CSDN 系列：`ask.csdn.net/8680132`、`tencentcloud.csdn.net/69eaaeb8...`）：audio_task 阻塞 → WDT → 重启的定位与喂狗/绑核解法。
- ESP32 语音方案 WiFi 共存与音频缓冲（CSDN `aiot` 系列）：双核资源争夺、I2S 时钟抖动、电源纹波致周期截断的硬件级对策。
- I2S 无声 + 看门狗（CSDN `m0_74274761`）：声道数错配 → 缓冲溢出 → WDT 重启的同构案例。

---

## 7. 2026-08-02 补充：设备收 GREET 后上行自断 + 双 WS 复用 session_id

> 针对 2026-08-02 14:43:49 实测的一通真实对讲（设备 `94a9901a8818`）。症状独立于 §2 的 B1/B2（既不是 AEC 杂音，也不是 20s 硬截止）。
>
> **给固件团队的阅读顺序**：先看 **7.5（最高嫌疑，机制已闭环）** → **7.4（已排除项，别再浪费时间）** → **7.6（errno 映射必须先核对，否则方向全错）** → **7.9（可执行步骤）** → **7.10（需要回传什么）**。
>
> ⚠️ **代码引用声明**：本节引用的固件代码来自**本机固件 `fuguoai-ble` v1.10.10 所用的同源基线**（`managed_components/78__esp-ml307/{tcp_transport,web_socket}.cc`、`main/protocols/websocket_protocol.cc`）。对讲固件 v1.6.0 由贵团队维护，**行号必然不同**，请以贵方代码为准核对**逻辑是否同构**，而非逐行比对。后端侧不碰固件代码（职责红线），本节只出证据与建议。

---

### 7.1 症状与双侧时间线对照

| 时刻 | 设备侧（`cap_v3.log`） | 服务端侧（159 容器日志 / `/metrics`） |
|----|----|----|
| 14:43:49 | — | 手机连入，`hello(role=phone, session=intercom, target=94a9901a8818)` 认证成功 |
| 14:43:49 | — | `[intercom.call] connected a=phone-Uc7d7cd08de20 b=94a9901a8818 (sent=True)` |
| 14:43:49 | 设备进入 intercom 态，打开音频 WS | 设备连入，`hello(session=intercom, format=opus,16000,1ch,20ms)` → 进入对讲透传 |
| 14:43:49 | **收到 `SERVER GREET(type=hello)`** | GREET 已下发（内容见 7.3） |
| 14:43:49 | — | `recv binary from phone len=640` → `relay registered=True state=established`；`手机→设备[pcm] in=640 opus_out=8 frames=98` |
| 14:43:49+ | `TcpTransport: Send failed errno 119` / `Receive failed errno 128` | **下行持续成功写入设备 WS** |
| 14:43:49–14:45:00 | `ws_conn=0` **持续 70 秒不恢复** | 同期 downlink 仍在增长，`send_error` 全程仅 **1** |
| 同期 | `SendAudio DROP not-connected` **3300+** | 上行 uplink 稀疏 → 会话被判死 |

**核心事实**：设备**单方面**认为连接断了并停发上行，但**服务端那条 TCP 连接自始至终是活的、可写的**。这不是"双方断链"，是**设备侧的连接状态标志被错误置死**。

---

### 7.2 服务端铁证：TCP 连接一直活着（这是定位的支点）

`/metrics`（2h 累计，实测）：

| 指标 | 值 | 含义 |
|----|----|----|
| `fuguoai_intercom_downlink_total` | **30219** | 服务端→设备成功写入 3 万+ 帧 |
| `fuguoai_intercom_uplink_total` | 18384 | 设备→服务端（含事故前正常时段） |
| `drop{reason=send_error}` | **1** | 3 万+ 次写入只失败 1 次 |
| `drop{reason=not_registered}` | 10030 | 会话被拆后的下游症状（服务端侧根因已修，见 7.8） |
| `conn_storm_guard` 命中（1013 拒连） | **0** | 服务端从未拒绝/踢掉该设备 |

**推论（决定性）**：若设备真的发了 TCP RST、或链路真的断了，服务端的 30219 次 `send` 不可能只失败 1 次——内核会立刻报 `EPIPE`/`ECONNRESET`，`send_error` 会暴涨。
⇒ **物理链路正常，服务端从未 abort。`ws_conn=0` 是设备固件内部的一个布尔标志被置死，而非 socket 真的断开。**

这一条把问题域从"网络/服务端"彻底收敛到"固件的连接状态机"。

---

### 7.3 服务端 GREET 原文（实测线上生效配置，非推测）

从 159 运行容器实读 `data/.config.yaml` 的 `fuguoai:` 段（即 GREET 模板）：

```json
{"type": "hello", "version": 1, "transport": "websocket",
 "audio_params": {"format": "opus", "sample_rate": 24000, "channels": 1, "frame_duration": 60}}
```

下发前服务端会做两步加工（`connection.py:372-373` + `helloHandle.py:178`）：
1. 注入 `session_id`；
2. **若客户端 hello 里带了 `audio_params`，则用客户端上报值覆盖**（回声语义）。

⇒ 设备 hello 自报 `opus/16000/1ch/20ms` ⇒ **设备实际收到的 GREET 里 `audio_params` 就是它自己报的 16000/20ms**，模板里的 `24000/60` 不会下发给该设备。

> 📌 **提示**：模板初值 `24000/60` 仅在客户端**完全不报** `audio_params` 时才会下发。若贵方某版本固件的某条 WS（例如 intercom 音频 WS）在 hello 里**省略了 `audio_params`**，它就会收到 `sample_rate=24000, frame_duration=60`，进而把 opus 解码器配错（与 §0 的 20ms/16k 不变式冲突，且 §0 已实证 60ms 有害）。**请核对 intercom 音频 WS 的 hello 是否带全 `audio_params`**——这是一个低成本、高收益的自查点。

---

### 7.4 已排除的假设（后端侧已实证否决，请勿重复排查）

| # | 假设 | 结论 | 依据 |
|---|----|----|----|
| H1 | GREET 缺 `transport` 字段 → 固件 `ParseServerHello` 提前 return、不置握手事件位 → 10s 超时 tear down | ❌ **排除** | 实测 GREET **含** `"transport":"websocket"`（见 7.3）。此前怀疑该项是因为同源基线里有一条兼容补丁注释「不再因为 transport 字段缺失/非 websocket 而放弃握手」，说明历史上确有此坑，但**本次不适用** |
| H2 | 服务端 `welcome_msg` 引用全局 config → 手机先连污染 `audio_params` → 设备收到错误 GREET | ❌ **排除** | `connection.py:117` 为 `self.config = copy.deepcopy(config)`，每连接深拷贝，无跨连接污染 |
| H3 | relay 仍要求设备带 4 字节 seq 头，不带就掐链路 | ❌ **彻底排除** | relay 已于 2026-08-02 切裸 opus，`connect()`/`relay()` 均不拼不剥 seq；`not_registered` 是注册缺口，**不是** seq 校验。**请勿再加回 seq 头** |
| H4 | 服务端主动 RST / 触发限流踢连 | ❌ **排除** | `conn_storm_guard` 命中 0；`send_error` 仅 1（见 7.2） |

---

### 7.5 🔴 最高嫌疑 H5：一次瞬时发送失败把 `connected_` **永久**置死

这是目前唯一能同时解释「设备 `ws_conn=0` 持续 70s」+「服务端下行 30219 帧照常成功」的机制。

**同源基线代码（`managed_components/78__esp-ml307/tcp_transport.cc`）**：

```cpp
int TcpTransport::Send(const char* data, size_t length) {
    int ret = send(fd_, data, length, 0);
    if (ret <= 0) {
        connected_ = false;              // ← ①任何 ret<=0 一律判死连接
        ESP_LOGE(TAG, "Send failed: %d", ret);
    }
    return ret;
}
```

**调用方（`web_socket.cc:369`）**：

```cpp
bool WebSocket::SendAllRaw(const void* data, size_t len) {
    auto ptr = (char*)data;
    while (transport_->connected() && len > 0) {   // ← ②connected_ 为假则一帧都发不出
        int sent = transport_->Send(ptr, len);
        if (sent < 0) return false;
        ...
    }
}
```

**因果链**：

```
对讲上行 50 帧/秒 持续高频写
        │
        ▼
某一次 send() 返回 <= 0（缓冲满 / lwIP pbuf 耗尽 / 瞬时拥塞）
        │                        ※ 此时 TCP 连接并未断开
        ▼
connected_ = false        ← 全代码路径中除 Connect() 外无任何地方复位
        │
        ▼
SendAllRaw 的 while 条件恒假 → 后续每一帧直接 DROP
        │
        ▼
ws_conn=0 持续 70s + SendAudio DROP not-connected 3300+
        │
        ▼
服务端侧：TCP 仍活 → downlink 30219 帧继续成功（send_error=1）
```

**为什么主对话 WS 不出问题、只有对讲 WS 出问题**：
- 主对话上行是**突发**的（用户说话时才发，且有 VAD 静音门控）；
- 对讲上行是**持续恒定** 50 帧/秒（20ms/帧）双向长时占用；
- ESP32 的 lwIP 发送缓冲（`TCP_SND_BUF`）与 pbuf 池（`PBUF_POOL_SIZE`）在持续高频小包场景下**远比突发场景更容易瞬时耗尽**。
⇒ 对讲是**唯一**能稳定触发这条路径的业务场景，与"只有对讲挂"的现象完全吻合。

**建议修复方向（供贵方评估，后端不越界改固件代码）**：
1. **区分可重试错误**：`send()` 返回 -1 时读取 `errno`，若为 `EAGAIN`/`EWOULDBLOCK`/`ENOBUFS`/`ENOMEM`，**不要**置 `connected_ = false`，改为短暂退避（如 `vTaskDelay(1~5ms)`）后重试；仅在 `EPIPE`/`ECONNRESET`/`ENOTCONN` 等**确定性致命错误**时才判死。
2. **`connected_` 需要可恢复路径**：当前一旦置 false 就永久不可逆，缺少自愈；建议置死时同步触发重连，而不是静默 DROP 3300+ 帧。
3. **加发送水位保护**：上行前检查发送队列水位，超阈值丢**最旧**帧而非判死连接（对讲丢一帧无感，断链却是致命的）。
4. **`ret == 0` 与 `ret < 0` 应区别对待**：当前 `ret <= 0` 合并处理，语义不同（0 通常非致命）。

---

### 7.6 ⚠️ errno 数值映射必须先核对，否则整个诊断方向会偏

设备日志写的是 `Send failed errno 119 (ECONNABORTED)`。**这个名字标注很可能是错的。**

ESP-IDF 使用 **newlib** 的 `errno` 数值，与 Linux **不同**：

| 数值 | newlib（ESP-IDF 实际） | Linux（若照抄 Linux 表会误读成） |
|----|----|----|
| 119 | `EADDRNOTAVAIL`（Can't assign requested address） | `ENAVAIL`（语义荒谬，显然不是） |
| 123 | **`ECONNABORTED`** | — |
| 128 | **`ENOTCONN`**（Socket is not connected） | — |
| 11 | `EAGAIN` / `EWOULDBLOCK` | 同 |

**两点判读**：
- `errno 128 = ENOTCONN` 在 newlib 下**对得上**，且与 7.5 完全自洽——`connected_` 被置死后代码仍对该 socket 调用 `recv()`，或 socket 已被本端 `close()`，自然报"未连接"。**这是结果，不是原因。**
- `errno 119` 在 newlib 下是 `EADDRNOTAVAIL`，**不是** `ECONNABORTED`（后者是 123）。若贵方的 errno→名字映射表是照抄 Linux 的，那么日志里所有 errno 名字都不可信，**会把排查引向完全错误的方向**。

**核对方法（30 秒）**：在固件里临时打一行

```c
ESP_LOGI("ERRNO_PROBE", "EAGAIN=%d EWOULDBLOCK=%d ENOBUFS=%d ENOMEM=%d "
         "ENOTCONN=%d ECONNABORTED=%d ECONNRESET=%d EPIPE=%d EADDRNOTAVAIL=%d",
         EAGAIN, EWOULDBLOCK, ENOBUFS, ENOMEM,
         ENOTCONN, ECONNABORTED, ECONNRESET, EPIPE, EADDRNOTAVAIL);
```

对照输出重新解读历史日志。**在这一步完成前，请不要基于 "(ECONNABORTED)" 这个标签下任何结论。**

> 若核对后 119 确实落在 `EAGAIN`/`ENOBUFS` 一族（即"缓冲暂时不可用"这类**可重试**错误），那么 7.5 的假设即被直接坐实，修复就是"不要把可重试错误当致命错误"。

---

### 7.7 双 WS 复用同一 session_id（服务端已容错，设备可不改）

- 实测：固件对「主对话 WS」与「对讲音频 WS」**复用同一个 `session_id`（`SiTeOpTeshcaCh`）**。
- 造成的**服务端侧**副作用（已修）：旧连接迟到断开时，其注销操作按 `session_id` 命中并**误删了活跃新连接**的登记 → intercom `_get_conn()` 返回 None → 下行 `peer_offline` + watchdog 判「设备假在线」下发 `SetIntercom stop` → 会话被拆 → 设备后续上行帧全部落入 `not_registered`（实测 10030 次）。
- **服务端修复（2026-08-02 已部署）**：每条 WS 连接生成唯一 `conn_uid`，注销/接管判据优先按 `conn_uid` 精确匹配（缺失时退化为原 `session_id` 行为，向后兼容）。commit `7701691`，镜像 `fuguoai-server:conn-uid-20260802-1528`。
  - `conn_uid` 是**纯服务端内部**概念，**不上 8000 WS 协议、固件完全无感、无需任何配合改动**。
- ⚠️ **此修复只消除"服务端误拆会话"，不解决 7.1/7.5 的设备侧上行自断**——那是两个独立故障，叠加在同一通对讲上。

**建议（非阻塞，诊断友好性改进）**：给 intercom 音频 WS 使用**独立 session_id**。好处是两端日志可精确关联、也排除设备侧把两条 WS 状态机/缓冲互相混淆的可能。服务端已容错，故此项**不强制**。

---

### 7.8 服务端侧本轮已做的事（供贵方了解边界）

| 项 | 状态 | 说明 |
|----|----|----|
| 移除 4B seq 头，改裸 opus | ✅ 已部署 | 与固件 2026-08-01 切裸 opus 对齐 |
| `conn_uid` 防双 WS 注册互踩 | ✅ 已部署 | 见 7.7，固件无感 |
| grace 重连恢复重发 `SetIntercom start` | ✅ 已部署 | 88595b4 |
| 首连自举 `_bootstrap_monitor` | ✅ 已部署 | 设备 2.5s 无上行且手机在发 → 重发 `SetIntercom start`（≤2 次） |
| relay METRICS 可观测 | ✅ 已上线 | `/metrics`（8002，无鉴权）可直接拉取，含 downlink/uplink/drop 分因子计数 |

⇒ **服务端侧当前无已知未修的对讲阻塞项**。剩余阻塞点在设备侧上行自断（7.5）。

---

### 7.9 固件侧可执行排查步骤（按优先级）

**Step 1 — 核对 errno 映射（前置，必做）**
按 7.6 打印一次 errno 常量表，重新解读历史日志中的 119/128。**这一步的结论决定后续方向。**

**Step 2 — 在 `TcpTransport::Send` 失败点抓全上下文（最高优先）**
在置 `connected_ = false` 之前补一条日志，至少包含：
```
ret / errno / fd_ / length / 距上次成功发送的间隔ms / 本条 WS 是主对话还是 intercom
```
目标是回答一个问题：**第一次 send 失败时的 errno 到底是什么？** 若是 `EAGAIN`/`ENOBUFS` → 直接坐实 7.5。

**Step 3 — 确认 `connected_` 置死后是否有恢复路径**
全局搜索 `connected_` 的所有写入点，确认除 `Connect()` 外是否存在复位。若无 → 一次瞬时失败即永久瘫痪上行，属确定性缺陷。

**Step 4 — 核对 intercom 音频 WS 的 hello 是否带全 `audio_params`**
见 7.3 提示。若缺失，会拿到 `24000/60` 的模板初值，与 20ms/16k 不变式冲突。

**Step 5 — 观察 lwIP 缓冲水位（若 Step 2 指向 ENOBUFS/ENOMEM）**
关注 `CONFIG_LWIP_TCP_SND_BUF_DEFAULT`、`CONFIG_LWIP_TCP_WND_DEFAULT`、`PBUF_POOL_SIZE`；对讲持续 50 帧/秒场景下，默认值可能偏小。

**Step 6 — 双 WS 资源争用交叉验证**
两条 WS 复用同一 `session_id` 的前提下，确认设备侧未把两条 WS 的 socket/缓冲/状态机混淆（例如后开的 intercom WS 是否会误关同 session 的另一条 WS）。

---

### 7.10 需要固件团队回传的交付物

1. **errno 常量表实测输出**（Step 1）——一行日志即可。
2. **首次 `Send failed` 的完整上下文**（Step 2）——特别是 `errno` 原始数值与是哪条 WS。
3. **`connected_` 全部写入点清单**（Step 3）——确认有无复位路径。
4. **intercom 音频 WS 的 hello 原文**（Step 4）——确认 `audio_params` 是否带全。
5. **一段带时间戳的设备侧日志**，覆盖 `SERVER GREET` 前 500ms 至首次 `DROP not-connected` 后 2s。

配套地，服务端可随时提供同一时刻的容器日志与 `/metrics` 快照做双侧对齐——**请给出精确到秒的时间点 + 设备 MAC**，我方即可复原服务端侧时序。

---

### 7.11 服务端握手不变式（勿动，改动需两侧同步）

| 项 | 值 |
|----|----|
| 服务端 GREET | 设备 hello 被接受后必发 `type=hello`（正常握手，**不是**异常信号），设备应接收并进入透传，而非 abort |
| 发设备帧格式 | **裸 opus**（4B seq 已于 2026-08-02 移除） |
| 发手机帧格式 | 640B 裸 PCM 无头 |
| 节奏 | 20ms/帧（§0 已实证 60ms 聚合有害，绝不回退） |
| opus 参数 | 16kHz / 1ch / 20ms / 24kbps，与 conversation 路径对齐 |
| `conn_uid` | 纯服务端内部，不上 WS 协议，固件无感 |

---

## 8. 2026-08-03 补充：下行静音 + relay peer_left 诊断模式（后端 relay 注册路径缺陷）

> 针对 2026-08-03 09:42:42 的完整对讲会话（板A 最新固件，7 秒）。症状：双端无声 + 短暂"对讲中"后 7 秒退回待机。
>
> **初判（上午，未看后端日志）**：手机未上行真语音 → 静音 + relay 6s 挂断。
> **修正（10:26，查阅 159 容器日志 + METRICS 后）**：真根因是**设备上行 opus 帧从未进入 relay**，
> 因 `connection.py:_route_message` 仅检查单连接的 `intercom_mode` 标志，
> 设备复用已有对讲音频 WS 时标志未设 → 上行帧落入 VAD/ASR 路径被静默丢弃。

### 8.1 铁证链路（双侧对照）

| 时间 | 设备（固件） | 后端（159） |
|----|----|----|
| 09:42:42 | `STATE: intercom` | — |
| 09:42:43 | — | 手机连入，认证通过，`intercom.call.connected` |
| 09:42:43 | — | phone 进 relay 透传模式，**`restored=None`**（设备不在 grace 中） |
| 09:42:44 | — | ⚠️ **bootstrap#1**：`SetIntercom start` **`dev_age=None`** |
| 09:42:44~47 | `downlink arrival 50fps`, `decoded rms=31~56` | — |
| 09:42:46 | — | ⚠️ **bootstrap#2**：`dev_age=None` |
| 09:42:48 | — | 手机持续上发 640B PCM 帧 |
| 09:42:48 | 收到 `peer_left` | — |
| 09:42:49 | WS 断 `errno 128`，主 WS 重连 | 🔴 **watchdog `dev_age=None`** → 杀会话 |
| 09:42:49 | `STATE: idle` | — |

**METRICS 铁证**（`/metrics` 8002 实读）：
- `relay_downlink_total{phone→device} 835` ✅ — 手机→设备转发正常
- **无 `relay_uplink_total`** ❌ — 设备→手机转发零
- `relay_drop_total{reason="not_registered"} 13` — 仅 13 次丢弃（说明帧根本没到 relay）
- docker logs 搜不到 `recv binary from 94a9901a8818` — 设备上行帧从未进入 intercom 透传路径

### 8.2 根因定位（已修，2026-08-03 部署）

`connection.py:_route_message` 第 579 行仅检查 `self.intercom_mode`：

```python
if getattr(self, "intercom_mode", False):
    await relay(self.device_id, message)
    return
# 落到这里 → VAD/ASR 判空
if self.vad is None or self.asr is None:
    return  # 静默丢弃！
```

设备对讲音频 WS 如复用已有通道（持久化打开），其 hello 可能不带 `session=intercom`，
`intercom_mode=False` → 上行 opus 帧越过 relay → VAD/ASR 为 None → 静默丢弃 →
relay `_last_frame_ts` 永不为设备更新 → `dev_age=None` → bootstrap 唤不回 → watchdog 6s 判假在线 → 杀会话。

**修法（commit 07d8dea，镜像 `fuguoai-server:intercom-is-session-20260803`）**：
`_should_relay` 判断在 `intercom_mode` 之外增加 `get_intercom_registry().is_in_session(self.device_id)` fallback。
即使单连接未设标志，只要设备在 `_by_device` 有登记，二进制帧也进 `relay()`。

### 8.3 诊断判据（更新：增加 dev_age + relay 到达日志双检）

| 判据 | 含义 | 方向 |
|----|----|----|
| 后端 `dev_age=None`（bootstrap/watchdog 日志）| 设备上行从未进 relay | **查后端 `_route_message` 是否把帧路由到了 relay**（看 docker logs 有无 `recv binary from <device_id>` 行） |
| 后端有 `recv binary from <device_id>` 但 `relay_drop{reason="not_registered"}` 大 | 设备到 relay 了但 `_by_device` 无登记 | 查 `start()` 是否成功注册 |
| 后端 `relay_uplink_total` 有值 | relay 正常转发设备上行 | 上行链 OK |
| `downlink decoded avg_rms < 100` | 解码后是静音 | 可能是上行源（手机 PCM）本身就是静音，或 relay PCM→opus 编码异常 |
| `peer_left < 8s` + `dev_age=None` | watchdog 杀会话（设备上行从未到 relay） | **先修 router → relay 路径** |

### 8.4 给 relay/小程序团队的转交格式

从双侧日志同时抽取（定位更精确）：
- 后端：`watchdog 判定设备假在线 dev_age=None`
- 设备：`downlink decoded avg_rms=31`

若后端无 `recv binary from 94a...` 行 → 问题在设备上行帧到 relay 的**路由路径**（非 relay 自身）。
若后端有 `recv binary` 但 relay_uplink=0 → 问题在 relay 转发/注册。

### 8.5 固件侧治标选项

同上，仅文档建议，由对讲固件团队自行评估。
