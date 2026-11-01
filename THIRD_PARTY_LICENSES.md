# 第三方组件许可清单 (Third-Party Licenses)

本仓库的**自有代码**归 fuguoai 所有（见 `LICENSE`）。以下为项目依赖或再分发的
第三方组件及其许可证。自有代码不覆盖这些组件。

## 再分发的第三方资产（vendored）

| 组件 | 路径 | 许可证 | 说明 |
|---|---|---|---|
| Silero VAD ONNX 模型 | `models/snakers4_silero-vad/` | MIT | 仅模型数据文件（`silero_vad.onnx` 等），由 `onnxruntime` 直接加载；上游 https://github.com/snakers4/silero-vad 。**不含其 Python 源码**（已移除）。 |

## 主要依赖（声明于 requirements.txt / 基础镜像）

| 组件 | 许可证 | 备注 |
|---|---|---|
| numpy | BSD-3-Clause | |
| onnxruntime | MIT | VAD/embedding 推理后端 |
| websockets | BSD-3-Clause | 设备 WS 协议 |
| opuslib_next | MIT | 对讲 opus 编解码 |
| pydub | MIT | 音频处理 |
| openai | MIT | LLM/TTS 客户端（可选外部 key） |
| edge_tts | MIT | 默认 TTS |
| httpx / aiohttp / aiohttp_cors | BSD/MIT | HTTP 服务 |
| ruamel.yaml | MIT | 配置解析 |
| loguru | MIT | 日志 |
| requests | Apache-2.0 | |
| bs4 (beautifulsoup4) | MIT | |
| mcp / mcp-proxy | MIT | 工具/MCP 集成（我们封装，非 vendored） |
| cnlunar | MIT | 农历 |
| PySocks | BSD | |
| chardet | LGPL-2.1 | |
| markitdown | MIT | |
| PyJWT | MIT | 鉴权 |
| psutil | BSD-3-Clause | 监控 |
| portalocker | BSD-3-Clause | 文件锁 |
| Jinja2 | BSD-3-Clause | |
| av (PyAV) | BSD-3-Clause | 纯 PyAV 转码（对讲，禁 ffmpeg 管道） |
| fastembed | Apache-2.0 | 维度⑤ 本地语义 embedding（Qdrant/bge-small-zh-v1.5，ONNX） |

## 上游派生来源（关键 — 已核实）

本仓库在 **`xiaozhi-esp32-server`**（作者 **xinnan-tech**，上游
https://github.com/xinnan-tech/xiaozhi-esp32-server ）之上 fork 演进。
**许可证：MIT（2026-10-28 实测上游 `LICENSE` 原文逐字确认，非 Apache-2.0）。**

### 1) 上游 fork 来源与历史归属（MIT 归属保留，当前应用代码已为 fuguoai 原生）

本仓库于 2026-07-18 在 `xiaozhi-esp32-server`（xinnan-tech）之上 fork 演进，故在此保留对上游的
MIT 归属声明（下方版权与许可文本，**合规必需、不可删**）。

需特别说明：fork 起点 `8f051a8` 之后，应用层代码（`fuguoai_core` / `fuguoai_plugins` / `app.py` /
`handle/*` / `providers/*` / `utils/*` 等）已持续重写与解耦。据 `UPSTREAM_DECOUPLING.md` 与
`docs/similarity-audit.md` 实测（`tools/scan_derivation_v2.py` 可复现）：**122 个上游同名文件已
全量逐一定性**（判据 C/R/F/N/X，未定性 X = 0）——63 件 LOGIC < 0.40 直接达标，59 件 ≥ 0.40 全部
具备显式判据（R 重写 / F 契约锁定 / C·门证 clean-room 重写 + 行为门交叉互认），全量 LOGIC 均值
**0.455**；**60 个行为门全绿（aggregate PASS = 1272, FAIL = 0）**；运行时 `*.py` 源码（含注释）
**已彻底无 xiaozhi 命名、全仓零命中**。

> ⚠️ **基线口径（防断章取义）**：任何相似度数字均以"已含复果改动的 fork 起点快照"为基线、
> **非纯上游**，且 fork 后新增模块计 0 重叠；相似度数字只能排序、不能定性（`LOGIC=1.000`
> 可能是 0 行空文件 / 导出壳 / ABC 契约地板，≠ 逐字抄）。即**当前运行时应用代码已为 fuguoai
> 原生、归 fuguoai 自有版权（见根目录 `LICENSE`）**。下方 MIT 文本仅作历史来源合规归属保留，
> 不构成对当前自有代码的许可覆盖。

```
MIT License

Copyright (c) 2025 xinnan-tech

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### 2) 基础镜像（上游 OSS fork — 换皮/改名版）

本仓库的**基础镜像不是自研**，而是上述 `xiaozhi-esp32-server`（作者 xinnan-tech，
镜像源 `ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server-base`）的**换皮/改名版**。

- 应用 Dockerfile（`/opt/fuguoai-server/Dockerfile` 与仓库 `Dockerfile`）原始上游即上述 xiaozhi 镜像；
  本仓库提交 `05660e8`（2026-07-25）将 base 标签统一改为 `fuguoai-server-base:server-base`，
  并在 `/tmp/Dockerfile.server` 中做改名（`mv /opt/xiaozhi-esp32-server /opt/fuguoai-server`）。
- 运行镜像 `recovered-20260725`（及后续所有 `docker commit` 快照）由 `docker import` 一个
  2026-07-25 的工作态 tar 包得到（单 7.1GB 层），其文件系统内容源自 xiaozhi-esp32-server。
- **构建源可追**：基础镜像内容 = xiaozhi-esp32-server OSS + 重命名；非隐藏/proprietary 代码。
- 容器内 `/opt` 现仅含 `fuguoai-server`（原 xiaozhi 应用目录已改名），pip 无 xiaozhi 包残留；
  但 Python 运行时、torch/onnxruntime 等重依赖仍来自该上游基础镜像。

### 3) fuguoai 自有部分（独立署名，不在 MIT 覆盖内）

fork（2026-07-18）之后由 fuguoai 新增/重写的子系统**不**受上游 MIT 覆盖，归 fuguoai 专有，例如：
家庭 RAG（本地语义检索）、任意打断（barge-in / server-side AEC）、湖北文旅知识库、B 方案语音提醒→设备日程、
声纹/方言路由、活字匣/天使心产品隔离、设备档案解耦层（`device_profiles`）等大量复果专属模块
（逐文件判据与证据链见 `UPSTREAM_DECOUPLING.md` 与 `docs/similarity-audit.md`）。这些模块以 fuguoai
自有版权发布，可闭源、可再许可。

> 合规要点：MIT 仅要求**派生骨架**保留上述 xinnan-tech 版权与许可声明；fuguoai 自有新增代码的
> 版权与许可由 fuguoai 独立主张（见根目录 `LICENSE`）。两端署名互不覆盖。

## 已从依赖清单移除的项（运行时不再需要）

- `torch` / `torchaudio`：原 sentence-transformers 栈依赖；容器卸载 torch 以避 OOM，
  embedding 改用 fastembed（ONNX）。已从 requirements 移除，避免与无 torch 运行时矛盾。
- `aioconsole`（GPL-3.0-or-later）：曾随 requirements 进入生产镜像（`app.py._drain_stdin`
  仅用其 `ainput` 消费 stdin）。为使 GPL 组件不入生产分发物，已改用标准库等价实现
  （`loop.run_in_executor` 包 `sys.stdin.readline`），并自 requirements 移除（2026-11-01）。
- `silero_vad`（官方 pip 包）：其 6.1.0 需 torch，本容器无 torch 无法 import；
  实际 VAD 由 `onnxruntime` + 自带 wrapper（`fuguoai_core/providers/vad/silero.py`）
  加载 vendored 的 `.onnx` 完成，故官方包未使用，已从 requirements 移除。

> 说明：本仓库的 `Dockerfile` 仅 `FROM fuguoai-server-base:server-base` + `COPY . .`，
> 自有业务代码（`fuguoai_core` / `fuguoai_plugins` / `fuguoai_config` / `app.py`）全部在 COPY 层之上，
> 100% 自有；基础镜像层（Python 运行时 + 重依赖）来自上游 xiaozhi-esp32-server OSS fork。

## 结论性声明

本仓库的运行时应用代码（`fuguoai_core` / `fuguoai_plugins` / `fuguoai_config` / `app.py` 等）
为 **fuguoai 自有版权**，不受上游 MIT 覆盖；上游 MIT 归属仅保留于历史 fork 来源与基础镜像的
合规披露（上文 §上游派生来源 §1–§2）。第三方组件许可按上述清单逐一对应；requirements 与
生产镜像当前**不含任何 GPL 组件**（aioconsole 已移除，见上节）。本清单随依赖与证据链更新
同步维护，实测口径以 `UPSTREAM_DECOUPLING.md` / `docs/similarity-audit.md` / `docs/behavior-gate.md`
为准。
