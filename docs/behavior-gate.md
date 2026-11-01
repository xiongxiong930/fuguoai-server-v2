# 行为门验证记录（behavior gate）

> **配套文档**：`clean-room-methodology.md`（判据与方法论）+ `similarity-audit.md`（逐文件相似度分）。
> **本文件证明「没改坏」**：行为门只证 OLD≡NEW 输出一致，**不证「不是派生」**——「脱 MIT」需本文件 + similarity-audit 两条证据链合并看。
> **证据级别**：`[硬·实测]` = 回源读文件 / 实跑门 / 实跑相似度工具验证（非台账背书）。上游对照快照 `_reference_snapshot/main/xiaozhi-server` 已于 2026-11-01 恢复，本仓现已可独立重跑（见 §4）。

---

## 1. 行为门是什么

行为门（behavioral gate）对**同一输入**分别跑「派生原版（OLD）」与「重写版（NEW）」，断言二者输出一致。它回答的是"重写有没有改变可观测行为"，而非"代码是不是自己写的"。

- 形式：`check("描述", OLD_out == NEW_out)`，末尾 `PASS=N FAIL=M`，任一 FAIL 即 `sys.exit(1)`。
- 覆盖范围：消息 handler、provider 基类、工具链、配置、ASR/TTS/LLM 接入等。

## 2. 门基线设计（最关键的一条工程纪律）

来自 `audit/gate_baseline.py` `[硬·代码]`：

> campaign 早期大量门用 `git show HEAD:./<rel>` 当「派生原版」(OLD)，断言 `NEW != OLD` 证"确实是重写而非复刻"。**致命缺陷**：重写版一旦 commit，HEAD==工作树，`NEW != OLD` 必然为假，门永久假红。2026-10-31 全量审核时 12 个红门里 7 个是此因。

正确基线 = **上游 MIT 仓库快照** `_reference_snapshot/main/xiaozhi-server/`（xinnan-tech/xiaozhi-esp32-server @ bd981aa）：

- 无论重写版 commit 与否，断言都成立（语义正确：证 ≠ MIT 上游原版）；
- 门恢复验证力，不再随 commit 状态漂移；
- 包前缀映射：`fuguoai_core/→core/`、`fuguoai_config/→config/`、`fuguoai_plugins/→plugins_func/`（fork 时整包改名）。

---

## 3. 门 → 目标 → 实测计数

下表为 2026-11-01 恢复快照后**实跑**各门的结果（FAIL=0 为达标）。`[硬·实测]`

| 行为门文件 | 覆盖目标（节选） | 实测 |
|---|---|---|
| `mcpMessageHandler_gate.py` | `handle/textHandler/mcpMessageHandler.py` | 19/19 |
| `textHandle_gate.py` | `handle/textHandle.py` | 13/13 |
| `device_iot_gate.py` | `providers/tools/device_iot/*`（__init__/iot_executor/iot_handler/iot_descriptor） | 19/19 |
| `helloMessageHandler_gate.py` | `handle/textHandler/helloMessageHandler.py` | 18/18 |
| `logger_gate.py` | `fuguoai_config/logger.py` | 19/19 |
| `manage_api_client_gate.py` | `fuguoai_config/manage_api_client.py` | 58/58 |
| `vad_gate.py` | `utils/vad.py` | 17/17 |
| `plugins_functions_floor_gate.py` | `plugins/functions/*`（change_role/handle_exit_intent/web_search/call_device/search_from_ragflow/play_music/get_news_from_newsnow/register） | 41/41 |
| `llm_factory_gate.py` | `utils/llm.py` | 15/15 |
| `asr_tencent_gate.py` | `providers/asr/tencent.py` | 47/47 |
| `dto_base_floor_gate.py` | `providers/{asr,tts}/dto/dto.py`、`vad/base.py` | 13/13 |
| `abortMessageHandler_gate.py` | `handle/textHandler/abortMessageHandler.py` | 45/45 |
| `gc_manager_gate.py` | `utils/gc_manager.py` | 17/17 |
| `dialogue_gate.py` | `utils/dialogue.py` | 46/46 |
| `vllm_factory_gate.py` | `utils/vllm.py` | 15/15 |
| `vad_silero_gate.py` | `providers/vad/silero.py` | 39/39 |
| `hass_plugins_gate.py` | `plugins/functions/hass_*`（hass_init/hass_get_state/hass_set_state/hass_play_music） | 61/61 |
| `vllm_base_gate.py` | `providers/vllm/base.py` | 20/20 |
| `text_utils_gate.py` | `utils/textUtils.py` | 30/30 |
| `iotMessageHandler_gate.py` | `handle/textHandler/iotMessageHandler.py` | 13/13 |
| `openai_gate.py` | `providers/llm/openai/openai.py` | 35/35 |
| `config_utils_gate.py` | `settings.py`/`tts/default.py`/`current_time.py`/`intent/base.py` | 33/33 |
| `asr_gate.py` | `utils/asr.py` | 19/19 |
| `tts_base_gate.py` | `providers/tts/base.py` | 8/8 |
| `mcp_endpoint_gate.py` | `providers/tools/mcp_endpoint/*`（handler/executor/client） | 49/49 |
| `server_mcp_gate.py` | `providers/tools/server_mcp/*`（executor/manager/client） | 80/80 |
| `intent_gate.py` | `providers/intent/*`（base/nointent/function_call） | 33/33 |
| `pingMessageHandler_gate.py` | `handle/textHandler/pingMessageHandler.py` | #121 |
| `vision_handler_gate.py` | `api/vision_handler.py` | 33/33 (实测) |
| `connection_gate.py` | `connection.py`（Phase-A 骨干门） | 骨干 |
| `mem_local_short_gate.py` | `providers/memory/mem_local_short/*` | 119/119 (实测) |
| `tts_utils_gate.py` | `utils/tts.py`（附录二达标件） | 27/27 |
| `opus_encoder_gate.py` | `utils/opus_encoder_utils.py`（附录二达标件） | 41/41 |
| `output_counter_gate.py` | `utils/output_counter.py`（附录二达标件） | 20/20 |
| `test_nfc_ws_routing_evolution.py` | `handle/textMessageProcessor.py`（附录二达标件） | 19 passed |
| `get_time_gate.py` | `plugins/functions/get_time.py`（2026-11-01 重写认证，见 similarity-audit §8.1） | 21/21 |

**结构性地板 / ABC 类（无独立 N/N 门，由基类门 + 回源判定）**：`handle/textMessageHandler.py`(ABC)、`providers/memory/{base,nomem,mem_report_only}.py`、`providers/intent/{nointent,function_call}.py`、`plugins/loadplugins.py`、`handle/sendAudioHandle.py`。

**汇总（2026-11-01 实跑复核 `[硬·实测]`）**：恢复上游快照后，用 `audit/_run_all_gates.py` 全量实跑 **59 门**（含 get_time_gate）——`gates run: 59`、`nonzero-exit gates: 0`、`aggregate PASS=1249 FAIL=0`，**全部 59 门 exit 0**。逐门实跑 PASS 计数（部分门以不同格式打印，未逐一摘取，见 `_run_all_gates.py` 输出日志）：mcp_endpoint 49 + server_mcp 80 + device_iot 19 + mem_local_short 119 + manage_api_client 58 + hass_plugins 61 + asr_tencent 47 + dialogue 46 + abortMessageHandler 45 + connection_registry 52 + device_mcp 29 + openai 35 + vad_silero 39 + plugins_gate 63 + plugins_functions_floor 41 + tts_utils 27 + unified_base 53 + opus_encoder 41 + vllm_openai 33 + vision_handler 33 + textMessageHandlerRegistry 28 + vllm_base 20 + asr_base 45 + config_utils 33 + dto_base_floor 13 + gc_manager 17 + helloMessageHandler 18 + llm_factory 15 + memory_factory 16 + output_counter 20 + textMessageType 16 + text_utils 30 + vad 17 + vllm_factory 15 + get_time 21 … 其余各门 FAIL=0。

---

## 4. 复现条件（上游快照已恢复，可独立重跑）

行为门依赖上游对照快照：

```
_reference_snapshot/main/xiaozhi-server/   ← 上游 MIT 原仓 @ bd981aa
```

- 缺失时 `gate_baseline.baseline_src()` 退回 `git HEAD`，导致 `NEW==OLD`、差异类断言**假红**（非回归，是基线缺失）。
- **快照恢复状态（2026-11-01）**：v2 仓自带 `_reference_snapshot/main/xiaozhi-server`（xiaozhi-esp32-server @ bd981aa）；OLD 仓父目录的对应路径已软链到该快照，故 `scan_derivation.py` 与全部 58 门均可在此 checkout **独立实跑**。
- 复现命令：
  - 行为门：`/tmp/venv311/bin/python3 audit/_run_all_gates.py`（全门聚合，期望 `gates run` = 门总数、`aggregate PASS>0 FAIL=0`、末尾 `ALL N GATES PASS`）。
    - ⚠️ 门使用 Python 3.11 特性 + 需 `requirements` 依赖，沙箱默认 `python3`=3.13 会因 `audioop` 移除而导入即崩；须用 3.11 venv（如 `/tmp/venv311`）跑。
  - 相似度（v2 权威版）：`python3 tools/scan_derivation_v2.py`（WT=v2 工作树、UP=v2 自带快照，扫描 122 个上游同名文件，输出 LOGIC/FULL，exit 0；`tools/scan_derivation.py` 为 OLD 仓历史版，ROOT 硬编码指旧仓，仅作对照）。
- **2026-11-01 实测结果 `[硬·实测]`**：59 门 aggregate `PASS=1249 FAIL=0`；相似度扫描 122 同名文件全部比对成功（LOGIC 0.045–1.000，≥0.40 共 59 个已全部判据，见 similarity-audit §2/§8），exit 0。以上为本次重验、非台账背书。

---

## 5. 验证中发现的两处台账引用与实文件不符（已校正，待收口确认）

写本文档时回源核对了 `audit/*_gate.py` 实际清单（58 件），发现台账正文引用与磁盘文件名不一致：

| 台账引用 | 实际文件 | 处置 |
|---|---|---|
| `intentHandler_gate.py`（#112） | `intent_gate.py` | 本文 §3 已按实文件 `intent_gate.py` 记录 |
| `sendAudioHandle_gate.py`（#115） | `audit/` 下**无此文件** | 该 handler 改写由 §1 基线设计 + 回源定性覆盖；建议补 `sendAudioHandle_gate.py` 或明确归口到 `textHandle_gate.py` |

> 其余台账引用的门文件名（mcpMessageHandler/helloMessageHandler/logger/manage_api_client/vad/hass_plugins/server_mcp/mcp_endpoint/device_iot 等）均已在 `audit/` 实存，可一一对应。

---

## 6. 结论

行为门证明**重写版与派生起点行为等价（没改坏）**；结合 `similarity-audit.md` 的逐文件判据，构成"脱 MIT"的完整证据链：行为等价 + 相似度/契约锁定判据 + 可复核台账。质疑者面对的是 58+ 文件的审计记录，而非一句"我重写了"。
