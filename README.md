# 复果AI 后端 · fuguoai-server

复果AI（FuguoAI）是面向家庭陪伴设备的**本地化 AI 对话服务端**：以低延迟全双工语音链路为核心，整合 ASR / LLM / TTS / VAD 等能力抽象，并内置设备管理、对讲中继、声纹识别、主动提醒、家庭知识库等自有业务模块。本仓库承载服务端全部逻辑与运维工具。

> 项目标识：`fuguoai-server` · 当前版本 `0.9.5`（首个正式 release `1.0.0` 过渡期，见 [RELEASE.md](RELEASE.md)）。

---

## 1. 能力概览

- **全双工语音对话**：设备端经 WebSocket（`/fuguoai/v1/`）上行音频、下行 TTS，服务端做 VAD → ASR → 意图 → LLM → TTS 的实时流水线；支持服务端 AEC 回声消除与打断（barge-in）。
- **能力抽象层（providers）**：ASR / TTS / LLM / VAD / 意图 / 记忆 / 嵌入 / 工具 均以接口归一，可按配置替换具体实现（如腾讯云、本地 vLLM、Edge-TTS 回落等）。
- **设备与家庭管理**：设备接入鉴权、设备覆盖配置、家庭成员、声纹（说话人识别）、家庭知识库（RAG）、人设（persona）、预设词。
- **对讲（Intercom）**：设备间中继通话，含会话登记、统计与自愈看门狗。
- **主动服务**：主动对话引擎、网络提醒（日程）、天气、OTA 升级下发。
- **可观测与运维**：`/metrics` 指标探针 + 容器级 `HEALTHCHECK` + 进程内看门狗；配置热加载；依赖月度审计。

---

## 2. 目录结构

```
fuguoai-server/
├── app.py                      # 入口：asyncio.run(main) → 启动 WebSocket(8000) 与 HTTP(8002)
├── Dockerfile                  # 基于预构建基础镜像 fuguoai-server-base:server-base，仅覆盖代码
├── docker-compose.yml          # 端口映射 8000(WS) / 8002(HTTP+metrics)
├── requirements.txt            # Python 依赖（推荐 Python 3.10 环境）
├── config.yaml / config_from_api.yaml   # 运行时配置
├── fuguoai_config/             # 版本与日志配置（SERVER_VERSION 在此）
├── fuguoai_core/               # ★ 核心服务代码
│   ├── websocket_server.py     # WebSocket 接入（/fuguoai/v1/）
│   ├── http_server.py          # HTTP 服务 + /metrics 指标页
│   ├── connection.py           # 设备连接与会话状态机
│   ├── auth.py / device_access.py / guards.py
│   ├── handle/                 # 上行/下行消息处理器（音频、意图、文本、abort…）
│   ├── api/                    # HTTP 路由：设备、admin、对讲、提醒、天气、声纹、OTA…
│   ├── providers/              # 能力抽象：asr tts llm vad intent memory embedding tools vllm
│   └── utils/                  # 公共工具：metrics 熔断 缓存 配置 调度 声纹…
├── fuguoai_plugins/            # 插件机制：functions / register / loadplugins
├── tools/                      # 运维与质量工具
│   ├── deploy_file.sh          # 单文件部署到容器
│   ├── regression_gate.py      # 回归门禁（P0/P1/P2/P3 分级）
│   ├── dep_audit.py            # 依赖审计（PyPI latest / CVE）
│   ├── similarity_report.py    # 自演化度量（基线=本仓 fork 起点）
│   ├── acceptance_probe.py     # 验收探针
│   └── PHASE5_PLAN.md          # 阶段计划
├── web/
│   └── device-panel/           # 设备面板前端
├── tests/                      # test_*.py 回归用例（run_tests.sh 自动归集）
├── models/ music/ docs/ data/  # 模型 / 音频资源 / 文档 / 运行时数据
├── CHANGELOG.md                # 变更日志
├── RELEASE.md                  # 版本体系与发版/审计规范
├── THIRD_PARTY_LICENSES.md     # 第三方许可与归属声明（务必保留）
└── UPSTREAM_DECOUPLING.md      # 上游残留清理清单（脱离上游影响）
```

---

## 3. 技术栈

| 领域 | 选型 |
| --- | --- |
| 运行时 | Python 3.10，异步（asyncio） |
| 接入层 | `websockets` / `aiohttp`（HTTP + CORS） |
| 语音 | Silero VAD、腾讯云 ASR、腾讯云 TTS（边合成边 opus）、opuslib 编码 |
| 大模型 | OpenAI 兼容接口 / 本地 vLLM；fastembed 嵌入 |
| 配置 | YAML（`ruamel.yaml`），热加载 |
| 可观测 | 自研 `/metrics` 指标 + loguru 分级日志 + 容器 HEALTHCHECK |
| 部署 | Docker（预构建基础镜像）+ docker-compose |

---

## 4. 运行方式

### 4.1 容器部署（推荐）

```bash
# 构建（基于已存在的 fuguoai-server-base:server-base 基础镜像）
docker build -t fuguoai-server:latest .

# 启动
docker compose up -d       # 暴露 8000(WS) / 8002(HTTP+metrics)
```

- WebSocket 对接地址：`ws://<host>:8000/fuguoai/v1/`
- HTTP / 指标：`http://<host>:8002/` ，指标页 `http://<host>:8002/metrics`

### 4.2 本地运行（开发）

```bash
pip install -r requirements.txt
export PYTHONPATH="$(pwd)"
python app.py                # 默认 WS 8000 / HTTP 8002
```

### 4.3 测试

```bash
bash run_tests.sh            # 自动发现并运行 tests/test_*.py 全部用例
```

回归门禁与依赖审计参见 `tools/regression_gate.py`、`tools/dep_audit.py`。

---

## 5. 配置与运维要点

- **版本号**：`fuguoai_config/logger.py` 的 `SERVER_VERSION`（设备/运维展示、日志头）。发版时同步更新并打 `v<MAJOR.MINOR.PATCH>` tag。
- **热加载**：`POST /admin/reload` 重载配置，无需重启。
- **健康检查**：容器 `HEALTHCHECK` 复用 `/metrics`；进程内看门狗覆盖事件循环冻死等盲区。
- **部署纪律**：改代码 → `py_compile` → 部署 → 清 `__pycache__` → 重启 → 校验活文件 md5/grep → 提交。详见 `tools/deploy_file.sh`。

---

## 6. 合规与归属

本项目在演进过程中复用了若干开源组件的能力，相关**第三方许可与版权归属**统一记录在 [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) 中，**请务必保留**，这是合规要求而非项目说明。

应用源码相对上游 xiaozhi-esp32-server 已完成 clean-room 解耦（2026-11-01 收口：122 文件逐一判据、真派生 X=0、59 行为门全绿），证据链与复现方式见 [`UPSTREAM_DECOUPLING.md`](UPSTREAM_DECOUPLING.md)、`docs/clean-room-methodology.md`、`docs/similarity-audit.md`、`docs/behavior-gate.md`。

如需了解本项目的独立演进脉络，另见 [`RELEASE.md`](RELEASE.md)。

---

## 7. 文档索引

- `CHANGELOG.md` — 版本变更明细
- `RELEASE.md` — 版本体系、发版与依赖审计规范
- `UPSTREAM_DECOUPLING.md` — 上游残留清理清单
- `tools/PHASE5_PLAN.md` — 当前阶段研发计划
- `tools/similarity_report.md` — 自演化度量报告（基线=本仓 fork 起点）
