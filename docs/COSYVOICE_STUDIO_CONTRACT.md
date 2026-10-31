# CosyVoice 工作室本地 TTS 对接契约

> 适用：复果 AI 后端 `fuguoai-server` 的 `CosyVoiceTTSProvider` 调用**工作室本机**部署的本地方言 TTS。
> 视角：本文档是**被调用方（工作室）**的实现契约；调用方（服务器端）代码已就绪。
> 更新：2026-10-14

## 1. 角色与边界

| 角色 | 职责 | 状态 |
| --- | --- | --- |
| 服务端（`fuguoai-server`） | 按设备 dialect 路由，HTTP 调用工作室机；方言后端不可达自动降级腾讯云 | ✅ 已就绪 |
| 工作室端（本文档） | 本机部署 HTTP 服务，加载方言音色模型，接收文本返回裸 PCM | ⬜ 待工作室实现 |
| 音色训练 / 说话人注册 | 由工作室用自有工具完成 | 不在范围内 |
| 服务端配置（`data/.config.yaml`） | 由后端侧负责 | 不在范围内 |

---

## 2. 部署形态

- **监听地址**：工作室自定义，建议 `0.0.0.0:10096`（端口可改）。
- **可达性**：仅需**局域网/内网**可达；服务端容器需能访问该 IP（注意跨主机/容器网络、防火墙）。
- **服务端指向**：通过配置 `TTS.CosyVoice.base_url` 指定，例如：
  ```yaml
  TTS:
    CosyVoice:
      base_url: http://<工作室机局域网IP>:10096   # 服务端填这里
      timeout_ms: 8000
      dialect_speakers: { "武汉": spk_wuhan, "宜昌": spk_yichang }
  ```

---

## 3. 接口契约

### `POST {base_url}/tts/stream`

> `endpoint` 路径可在服务端 `TTS.CosyVoice.endpoint` 配置，默认 `/tts/stream`。
> 服务端实现为**一次性 POST 读取完整响应**（读全后按设备率重采样），因此工作室**只需一次性返回完整 PCM 即可，无需实现真流式/SSE 分块协议**。

**请求**

- `Content-Type: application/json`
- Body（JSON）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | string | 是 | 待合成文本。服务端已做 Markdown 清洗与替换词处理，**工作室无需二次清洗** |
| `audio_format` | string | 是 | 固定 `"pcm"` |
| `sample_rate` | int | 是 | 固定 `16000`（服务端期望的源采样率） |
| `speaker` | string | 否 | 说话人 id。服务端按 dialect 映射后透传；缺省时工作室用默认说话人 |

示例：
```json
{
  "text": "今天武汉有点闷热，记得多喝水",
  "audio_format": "pcm",
  "sample_rate": 16000,
  "speaker": "spk_wuhan"
}
```

**成功响应**

- `HTTP 200`
- `Content-Type: application/octet-stream`
- **Body：原始 16k / 单声道 / signed 16-bit little-endian PCM 字节流**
  - 无 WAV 头、无容器、无打包，纯裸 PCM。
  - 字节数 = `秒数 × 16000 × 2`（例如 1.0s → 32000 字节）。

**失败响应**

- 任意**非 200**（4xx / 5xx），Body 任意（建议 `{"error": "..."}`）。
- 服务端据此判定"工作室不可用"，**自动降级到腾讯云 TTS**，设备不静音。

---

## 4. 编码约束（硬指标，务必满足）

| 项 | 要求 | 服务端处理方式 |
| --- | --- | --- |
| 编码 | **PCM signed 16-bit LE** | 直接按 16k s16le 重采样，**不接受 mp3/wav 容器** |
| 声道 | **单声道 (mono)** | — |
| 采样率 | **16000 Hz** | 重采样到设备率（通常 24000）；若工作室用其他率，须在服务端 `TTS.CosyVoice.sample_rate` 同步配置 |
| 字节序 | **little-endian** | — |
| 文件头 | **无**（纯裸 PCM） | 有 WAV 头会被当成音频数据导致爆音/错位 |

> ⚠️ 若工作室引擎只输出 wav/其他格式：返回前**剥掉文件头并转成 16k s16le 裸 PCM**。

---

## 5. 方言与说话人

- 服务端 `dialect → speaker` 映射（示例，由后端 `TTS.CosyVoice.dialect_speakers` 配置，工作室按需对齐 id）：

  | dialect | speaker id |
  | --- | --- |
  | `武汉` | `spk_wuhan` |
  | `宜昌` | `spk_yichang` |

- 工作室侧：维护 `speaker id → 具体模型/音色` 的映射；收到 `speaker` 即加载对应方言音色合成。
- `speaker` 缺省（未传/未命中）→ 用工作室**默认说话人**（建议一个通用普通话或默认方言音色）。
- 服务端保证：方言设备请求必带正确 `speaker`；普通话设备走腾讯云、不会打到本接口。

---

## 6. 超时与并发

- **超时**：服务端默认 8s（`timeout_ms` 可配）。工作室应在数秒内返回，避免触发降级。
- **并发**：服务端多设备可能同时请求；工作室需支持**并发合成**（模型常驻显存 + 请求级线程池/队列）。
- **单请求**：服务端逐句顺序合成，每句一次请求；工作室每次请求独立完成即可。

---

## 7. 工作室侧最小实现示例（Python，框架无关）

仅示意契约，实际 TTS 引擎由工作室选择（CosyVoice / GPT-SoVITS / 其他）。

```python
# cosyvoice_server.py —— 示意，非生产代码
from fastapi import FastAPI, Request, Response
import your_tts_engine   # 工作室自有：加载模型、synthesiz()

app = FastAPI()

# speaker id -> 音色/模型 的映射由工作室维护
SPEAKERS = {"spk_wuhan": ..., "spk_yichang": ..., None: <默认音色>}

@app.post("/tts/stream")
async def tts(req: Request):
    body = await req.json()
    text = body["text"]
    speaker = body.get("speaker")            # 可能为 None
    # 合成：返回 16k / mono / s16le 裸 PCM bytes（无文件头）
    pcm = your_tts_engine.synthesize(
        text=text, speaker=speaker, sample_rate=16000
    )
    return Response(content=pcm, media_type="application/octet-stream")
```

要点：
- 若引擎输出 wav/其他：返回前 `pcm = strip_header_and_resample(raw, 16000, "s16le")`。
- 模型常驻内存/显存，避免每次请求重新加载。
- 做好异常捕获：合成失败时返回非 200，让服务端降级（不要返回 200 + 空音频，否则设备静音）。

---

## 8. 联调自测清单

- [ ] 工作室服务起在 `:10096`，`curl -X POST .../tts/stream` 能通。
- [ ] 发 `{"text":"你好","audio_format":"pcm","sample_rate":16000,"speaker":"spk_wuhan"}` → 返回 `200` + 裸 PCM 字节；用 `xxd | head` 确认**无 `RIFF` 头**、长度 = 秒数×32000。
- [ ] 不传 `speaker` → 用默认音色，仍 `200` + 合法 PCM。
- [ ] 关掉工作室服务 → 服务端日志出现 `CosyVoice 连接失败(...)` → 方言设备播报**自动走腾讯云**（不静音）。
- [ ] 服务端 `data/.config.yaml` 配置 `selected_module.TTS: router` + `TTS.CosyVoice` + `TTS.router`，`docker restart fuguoai-server` 后，武汉/宜昌设备语音为工作室方言音色、普通话设备为腾讯云音色。

---

## 9. 与服务端降级的配合

- 工作室**不可达 / 超时 / 报错** → 服务端自动降级腾讯云，设备照常出声（普通话音色）。
- 工作室**正常** → 方言设备用本地音色（更自然、零云成本、不占腾讯云配额）。
- 切换无感：服务端按设备 dialect 实时路由，仅服务端改配置 + 重启即可生效，设备无需重启。
