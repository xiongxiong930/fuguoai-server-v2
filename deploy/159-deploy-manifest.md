# 159 生产部署清单（deploy manifest）

> 用途：记录 159 生产实例（`ubuntu@<PROD_HOST>`，容器 `fuguoai-server`）的**非机密**特异性
> 配置，便于跨机重建 / 灾备复现。本文件**不含任何密钥**；密钥由宿主机 bind mount 管理，严禁入 git。

## 1. 配置耐用性拓扑（关键事实，纠正旧判）

- 容器 `/opt/fuguoai-server/data` **bind mount 到宿主机** `/opt/fuguoai-server/data`（RW）。
  因此 `data/.config.yaml`（生产配置真源）**存活于宿主机**，与镜像层无关：
  - `docker restart` 不丢；
  - 从任意镜像 `docker run` 也不丢（读宿主机同一份）；
  - `docker commit` **不会也不需要**捕获它。
- 结论：**"配置重部署会丢"不成立**——配置是最耐久的部分。它未进 git 仅影响"换机重建 159"
  时的可复现性，不影响当前运行。
- 代码改动（`fuguoai_core/...`）在镜像层，需 `docker cp` + `docker commit` 固化。
  例：token 落库修复 `da70742` → 镜像 `fuguoai-server:usage-tokfix-20260925`（2026-09-25）。

## 2. 非机密 · 159 特异性覆盖（在 `data/.config.yaml` 中、仓库模板 `config.yaml` 不含）

### 2.1 降本专项 4 开关（本次重点）
```yaml
ToolGate:
  enabled: true
MemoryRAG:
  enable_rag: true
  rag_top_k: 8
  context_window:
    max_rounds: 6
    max_history_chars: 2500
dialogue_eval:
  enabled: false
  llm: OpenAILLM
FamilyRAG:
  enabled: true
  embedding:
    type: local-st
  extract:
    enabled: false
```

### 2.2 对讲服务端 AEC 调参（159 特异性，固件 8818 联调用）
```yaml
fuguoai:
  intercom:
    server_aec: true
    server_aec_mode: 2ch_srvref
    server_aec_devices: [94a9901a8818]
    aec_max_delay_ms: 12000
    aec_echo_delay_ms: 0
    aec_echo_delay_band_ms: 0   # 2026-09-12 由 2500 改 0：config 非 0 会屏蔽 profile 的 band
    server_aec_phone: true
    intercom_aec_diag: true
aec_max_delay_ms: 12000
```

### 2.3 barge-in / 连接守卫（159 特异性）
```yaml
bargein_playing_grace_ms: 10000
bargein_long_play_guard_ms: 3000
bargein_disabled_devices: [94a9901a90c0]
bargein_semantic_gate_enabled: true
bargein_playing_grace_ms_per_device:
  94a9901a90c0: 800
conn_storm_guard:
  exempt_devices: [94a9902ee268]
```

### 2.4 模块选型 / 端口（159 生产值，模板为占位）
```yaml
server:
  http_port: 8002          # 模板 8003（metrics/OTA 走 8002）
selected_module:
  LLM: OpenAILLM            # 模板 ChatGLMLLM
  TTS: EdgeTTS              # 模板 TencentTTS
  Memory: mem_local_short   # 模板 nomem
```

## 3. 密钥清单（仅列键路径；值存活于宿主机 `data/.config.yaml`，**不在 git / 不在镜像 commitable**）

- `ASR.TencentASR.secret_id` / `secret_key`
- `LLM.OpenAILLM.api_key` / `fallback_api_key`
- `LLM.OpenAILLM-Mem.api_key`
- `TTS.TencentTTS.appid` / `secret_id` / `secret_key`
- `miniprogram.jwt_secret`（生产已设；模板为空）
- `voiceprint.secret_id` / `secret_key` / `group_id: fuguoai_family_02`
- `server.auth.allowed_devices`（白名单 MAC 列表）

## 4. auth_key 行为注记

- `data/.config.yaml` 当前**未设** `auth_key`；每次 `docker restart` 启动日志报
  "auth_key 未配置...生成随机 auth_key"。
- 因 `server.auth.enabled: false`，该随机 key 不影响设备鉴权；实测设备重启后自动重连成功。
- 若未来开启 `server.auth.enabled: true`，须在 `data/.config.yaml` 显式固定 `auth_key`，
  否则重启即失效。建议届时固定为一个随机长字符串（由宿主机管理，不入 git）。

## 5. 灾备复现步骤（换机 / 宿主机丢失时）

1. 目标机准备宿主机目录 `/opt/fuguoai-server/data/`。
2. 拉取本仓库代码，按 `usage-tokfix-20260925` 同等镜像起容器，bind mount 上述目录。
3. 将本清单 §2 的非机密覆盖 + §3 的密钥（从保险库 / 原机导出）写入 `data/.config.yaml`。
4. `docker restart` 校验 `healthz` / `metrics=200`。
