# 相似度审计（similarity audit）

> **配套文档**：`clean-room-methodology.md`（判据与方法论）+ `behavior-gate.md`（行为门验证记录）。
> **数据源**：`audit/EVIDENCE_LEDGER.md`（主证据台账）。所有分数均为 `measure_sim` 对比**上游 MIT 原仓** `xinnan-tech/xiaozhi-esp32-server @ bd981aa`。
> **证据级别**：`[硬·实测]` = 回源读文件 / 实跑门 / 实跑相似度工具验证（非台账背书）。本文件所有 LOGIC 数字与覆盖率已由 2026-11-01 恢复的快照 + `scan_derivation.py` 实跑复核确认（见 §2 复跑框）；C/R/F 判据标签仍源自 campaign 台账，但其事实底座（LOGIC）已实跑验证。

---

## 1. 度量口径

- `LOGIC` / `FULL` = canonical 归一化（去空白、注释、命名前缀 `fuguoai_`）后与上游同名文件比对。
- **C 判据有两种成立路径**（2026-11-01 全量审查时显式化，此前两义混用一个字母）：
  - **C·门证**（主表 16 行）：clean-room 重写完成 + 行为门交叉互认等价。LOGIC **不作阈值要求**——重写后仍 ≥0.40 者源于结构地板/契约锁定，由门证背书（如 `textHandle.py` 0.963 C，13/13 门）。
  - **C·阈值**（§8.3 的 52 件 + §4 附录 5 件）：`LOGIC < 0.40` 即视为重写达标，无需独立门。
  - `≥ 0.40` 且**未重写**的必须**回源定性**（R 契约锁定 / F 结构性地板 / X 真派生）。
- **铁律一**：相似度数字只能排序，不能定性。`LOGIC=1.000` 可能是 0 行空文件 / 导出壳 / ABC（契约地板），≠ 逐字抄。

## 2. 覆盖率

> **[硬·实测] 2026-11-01 全量复跑 + 122 文件逐一映射**：用 `tools/scan_derivation_v2.py`（v2 权威版：WT=v2 工作树、UP=v2 自带快照 @bd981aa、`git -C` 指 v2 仓）实跑，exit 0：
> - 扫描到「上游有同名」的工作树文件：**122** 个，全部比对成功；LOGIC 全区间 **0.045–1.000**；
> - 其中 **LOGIC≥0.40 共 59 个**（58 个已判据见 §3 主表 + 1 个 `protocol/falp/__init__.py` 判 N 复果自有，见 §8）；
> - **同路径核验**：59 个按上游同路径逐一核验，剩余 53 个此前未判据文件中 52 个 <0.40（阈值达标 C）+ 1 个 `get_time.py`（0.417，**真漏网，已于 2026-11-01 clean-room 重写**至 0.327 + 新增 `get_time_gate.py` 21 断言全绿，见 §8）；
> - 6 个为**基名碰撞假阳性**（`__init__.py`/`native/*`/`embedding/*` 上游无同路径文件，匹配自上游同名基名文件）→ 判 N 复果自有（见 §8）。
>
> **缺口（X/N/O）= 0 现为完备结论**：122 = §3 主表 58 + §4 附录 5 + §8 新映射 59（52 阈值达标 C + get_time 重写 C + 6 假阳性 N）。此前"58 文件、缺口 0"系台账子集口径，全集映射后口径统一为 122。

- 上游同名文件全集：**122**（2026-11-01 实跑 `scan_derivation_v2.py`）
- LOGIC≥0.40（需判据）：**59**（58 主表 C/R/F + 1 falp=N）
- 已判据（C/R/F/N）：**122/122** ✅（全集映射完成，见 §3/§4/§8）
- 真派生（X）：**0**（唯一 ≥0.40 漏网 get_time.py 已重写达标）
- 重写达标（`<0.40`）文件从主表消失，留证见 §4 附录 + §8 映射表。

---

## 3. 主表（58 文件，按 canonical LOGIC 降序）

> **数字口径**：LOGIC/FULL 为 **2026-11-01 `scan_derivation_v2.py` 对 v2 工作树实跑值**（[硬·实测]）；campaign 台账（OLD 仓时点）个别文件有 ≤0.03 漂移（connection 0.717→0.700、dialogue 0.590→0.569 等），**判定均不变**。判据含义见 §1（C 分 C·门证 / C·阈值 两路）。

| # | 文件 | LOGIC | FULL | 判据 | 达标 / 豁免理由 |
|---|------|------:|-----:|:---:|------|
| 1 | `providers/tools/__init__.py` | 1.000 | 1.000 | R | 0 行空文件（1 字节），空-空比较数学必然，不含任何代码（原误标 X 逐字）★纠正 |
| 2 | `providers/tools/mcp_endpoint/__init__.py` | 1.000 | 0.956 | R | 24 行纯导出壳，`from .x import Y`+`__all__`，导出名被调用方锁定（原误标 X）★纠正 |
| 3 | `providers/tools/device_mcp/__init__.py` | 1.000 | 0.955 | R | 24 行纯导出壳，无业务逻辑（原误标 X）★纠正 |
| 4 | `providers/tools/server_plugins/__init__.py` | 1.000 | 0.940 | R | 5 行纯导出壳（原误标 X）★纠正 |
| 5 | `providers/tools/server_mcp/__init__.py` | 1.000 | 0.894 | R | 10 行纯导出壳（原误标 X）★纠正 |
| 6 | `handle/textHandler/mcpMessageHandler.py` | 0.968 | 0.832 | F | 30 行极小委派 handler，字段名/委派签名与上游必然重叠（结构性地板） |
| 7 | `handle/textHandle.py` | 0.963 | 0.789 | C | 重写达标，行为门交叉互认（13/13） |
| 8 | `providers/tools/device_iot/__init__.py` | 0.962 | 0.916 | R | 12 行纯导出壳（原误标 X）★纠正 |
| 9 | `providers/tools/base/__init__.py` | 0.928 | 0.850 | R | 6 行 ToolType/ToolDefinition/ToolExecutor，HEAD 已是重写版（原误标 X）★纠正 |
| 10 | `handle/textHandler/helloMessageHandler.py` | 0.927 | 0.673 | C | 重写达标，派生起点非逐字相同（18/18） |
| 11 | `fuguoai_config/logger.py` | 0.923 | 0.838 | C | 前序已重写，本轮补门证无回归（19/19） |
| 12 | `fuguoai_config/manage_api_client.py` | 0.921 | 0.906 | R | 契约锁定：端点路径/业务错误码/重试状态码/JSON 键由 manage 服务契约锁定（58/58） |
| 13 | `utils/vad.py` | 0.892 | 0.883 | C | 重写达标（17/17） |
| 14 | `plugins/functions/change_role.py` | 0.884 | 0.829 | R | 契约锁定：LLM 工具 schema + 人设文案 + conn 首形参约定（41/41） |
| 15 | `utils/llm.py` | 0.874 | 0.765 | R | 契约锁定：create_instance 公开名 + providers/llm/<type> 路径 + 模块命名锁定（15/15） |
| 16 | `providers/asr/tencent.py` | 0.856 | 0.861 | C | 重写达标，TC3 签名逐字 + 多分支（47/47） |
| 17 | `plugins/functions/handle_exit_intent.py` | 0.852 | 0.728 | R | 契约锁定：LLM 工具 schema + close_after_chat 体结构（41/41） |
| 18 | `providers/asr/dto/dto.py` | 0.810 | 0.653 | R | 契约锁定：InterfaceType 枚举成员名/值锁定（13/13） |
| 19 | `handle/textMessageHandler.py` | 0.761 | 0.455 | F | 24 行 `TextMessageHandler(ABC)`，抽象方法签名被全部子类锁定（结构性地板） |
| 20 | `providers/tools/base/tool_types.py` | 0.755 | 0.673 | R | 33 行 ToolType 枚举 + ToolDefinition 数据类，成员/字段名被数十调用方锁定（原误标 X）★纠正 |
| 21 | `plugins/register.py` | 0.741 | 0.706 | R | 契约锁定：ToolType/Action/register_function 被装饰器与 connection 锁定（41/41） |
| 22 | `providers/vad/silero.py` | 0.735 | 0.667 | C | 重写达标，假 onnxruntime 隔离（39/39） |
| 23 | `connection.py` | 0.700 | 0.610 | C | 2400+ 行/40+ 反向依赖；保留行为门 + 逐段收口（Phase-A 骨干门） |
| 24 | `providers/memory/nomem/nomem.py` | 0.700 | 0.627 | F | 24 行空实现桩，方法名由 MemoryProviderBase ABC 锁定（结构性地板） |
| 25 | `providers/tools/base/tool_executor.py` | 0.700 | 0.649 | R | 33 行 ToolExecutor ABC，抽象方法被 5 子类锁定（原误标 X）★纠正 |
| 26 | `utils/vllm.py` | 0.667 | 0.588 | R | 契约锁定：create_instance + providers/vllm/<type> 路径 + sys.modules 缓存（15/15） |
| 27 | `providers/memory/mem_report_only/mem_report_only.py` | 0.667 | 0.113 | F | 25 行空实现桩，方法名由 ABC 锁定（结构性地板） |
| 28 | `plugins/functions/web_search.py` | 0.657 | 0.682 | R | 契约锁定：metaso/tavily/bing API URL + 字段名（41/41） |
| 29 | `handle/textHandler/pingMessageHandler.py` | 0.657 | 0.430 | C | 重写达标（0.430/0.655，门 #121） |
| 30 | `providers/memory/base.py` | 0.654 | 0.592 | R | 45 行 MemoryProviderBase ABC，抽象方法被 3 子类锁定（契约锁定） |
| 31 | `providers/intent/nointent/nointent.py` | 0.647 | 0.234 | F | 25 行恒定返回协议串 `continue_chat`（协议常量即契约，结构性地板） |
| 32 | `providers/intent/function_call/function_call.py` | 0.633 | 0.236 | F | 26 行恒定返回 continue_chat 协议串（结构性地板） |
| 33 | `handle/textHandler/abortMessageHandler.py` | 0.622 | 0.098 | F | 结构性地板（0.622），行为门认证等价（45/45） |
| 34 | `utils/gc_manager.py` | 0.604 | 0.508 | R | 契约锁定：公开 API 被 app.py 锁定 + GC 算法骨架强制（17/17） |
| 35 | `handle/sendAudioHandle.py` | 0.602 | 0.603 | C | 重写达标（门 #115；MQTT 16 字节头部 struct.pack 拼装，修上游 late-binding 坑） |
| 36 | `utils/dialogue.py` | 0.569 | 0.563 | C | 重写达标，四段拼装/窗口滑动压缩（46/46） |
| 37 | `plugins/functions/call_device.py` | 0.585 | 0.626 | R | 契约锁定：设备调用 LLM 工具 schema + 指令体（41/41） |
| 38 | `plugins/functions/hass_play_music.py` | 0.581 | 0.575 | F | 结构性地板：剥 LLM schema+imports 后仅剩 194 token 可改（门 61/61） |
| 39 | `providers/vllm/base.py` | 0.577 | 0.345 | C | 12 行骨架重写达标 0.322（20/20） |
| 40 | `plugins/loadplugins.py` | 0.583 | 0.395 | F | 25 行标准库扫描包内模块唯一正确写法（importlib+pkgutil），函数名被 connection 锁定（结构性地板） |
| 41 | `providers/tts/dto/dto.py` | 0.555 | 0.589 | R | 契约锁定：SentenceType/ContentType/InterfaceType 枚举 + TTSMessageDTO 字段（13/13） |
| 42 | `providers/vad/base.py` | 0.520 | 0.439 | R | 契约锁定：VADProviderBase.is_vad 签名被调用方锁定（13/13） |
| 43 | `utils/textUtils.py` | 0.494 | 0.435 | R | 契约锁定（协议数据表地板）；剥 EMOJI_MAP/EMOJI_RANGES/标点集后 LOGIC-LOCK **0.283**（30/30） |
| 44 | `fuguoai_config/settings.py` | 0.495 | 0.546 | R | 契约锁定：面向用户的错误文案逐字锁定（改一字即改排障指引） |
| 45 | `handle/textHandler/iotMessageHandler.py` | 0.488 | 0.437 | F | 结构性地板（0.487），行为门认证等价（13/13） |
| 46 | `providers/llm/openai/openai.py` | 0.479 | 0.542 | C | 重写达标，假流 yield 等价 + 备用通道降级（35/35） |
| 47 | `providers/intent/base.py` | 0.478 | 0.529 | R | 契约锁定：IntentProviderBase 抽象契约锁定（33/33） |
| 48 | `plugins/functions/hass_set_state.py` | 0.470 | 0.484 | R | 剥 schema+imports 后 LOGIC-LOCK **0.297**（门 61/61） |
| 49 | `plugins/functions/search_from_ragflow.py` | 0.453 | 0.434 | R | 契约锁定：RAGFlow 检索 API URL + 字段 + 工具 schema（41/41） |
| 50 | `utils/asr.py` | 0.467 | 0.492 | C | 重写达标，git HEAD 派生版 vs 新版交叉互认（19/19） |
| 51 | `providers/tts/default.py` | 0.451 | 0.402 | R | 契约锁定：DefaultTTS 类名/方法被锁定（33/33） |
| 52 | `utils/current_time.py` | 0.444 | 0.400 | R | 契约锁定：get_current_time_info + cnlunar API + strftime 格式（33/33） |
| 53 | `providers/tts/base.py` | 0.453 | 0.421 | R | 契约锁定：公共方法名/签名 + 抽象 text_to_speak（8/8） |
| 54 | `plugins/functions/hass_get_state.py` | 0.441 | 0.421 | R | 剥 schema+imports 后 LOGIC-LOCK **0.364**（门 61/61） |
| 55 | `plugins/functions/get_news_from_newsnow.py` | 0.432 | 0.373 | R | 契约锁定：NewsNow 聚合 API URL + 字段 + 工具 schema（41/41） |
| 56 | `handle/intentHandler.py` | 0.422 | 0.351 | C | 重写达标（0.424/0.351，门 #112） |
| 57 | `plugins/functions/play_music.py` | 0.423 | 0.408 | R | 契约锁定：LLM 工具 schema + 播放指令体（41/41） |
| 58 | `api/vision_handler.py` | 0.403 | 0.339 | C | 重写达标，双 <0.40（门 #120） |

---

## 4. 附录：达标件留证（重写后 `LOGIC<0.40`，主表外）

> `audit_campaign.py` 只报 `LOGIC≥0.40`，故达标文件从主表消失，此处单列留证 `[硬·实测]`。

| 文件 | 重写前 LOGIC | 重写后 LOGIC | FULL | 门 / 测试证据 |
|------|------------:|------------:|-----:|------|
| `utils/tts.py` | 0.625 | **0.240** | 0.237 | `tts_utils_gate.py` 27/27 + TTS 测试 62 passed |
| `utils/opus_encoder_utils.py` | 0.463 | **0.100** | 0.234 | `opus_encoder_gate.py` 41/41（真实 opuslib 逐字节） |
| `utils/output_counter.py` | 0.566 | **0.251** | 0.094 | `output_counter_gate.py` 20/20 |
| `handle/textMessageProcessor.py` | 0.403 | **0.358** | 0.385 | `test_nfc_ws_routing_evolution.py` 19 passed |
| `plugins/functions/hass_init.py` | 0.494 | **0.126** | 0.081 | `hass_plugins_gate.py` 61/61 |

## 5. 附录：契约锁定量化（`contract_floor.py` 剥契约后 LOGIC-LOCK）

| 文件 | LOGIC | 剥掉的契约 | LOGIC-LOCK | 结论 |
|------|-----:|------|-----:|------|
| `utils/textUtils.py` | 0.495 | EMOJI_MAP+EMOJI_RANGES+标点集（各 30–32% token） | **0.283** | 达标 |
| `plugins/functions/hass_set_state.py` | 0.471 | `*_function_desc`+imports（20.0%） | **0.297** | 达标 |
| `plugins/functions/hass_get_state.py` | 0.438 | `*_function_desc`+imports（22.8%） | **0.364** | 达标 |
| `plugins/functions/hass_play_music.py` | 0.583 | `*_function_desc`+imports（36.6%） | 0.497 | 结构性地板：剥后仅剩 194 token，可改写约 30 token |

> **读法**：LOGIC 高 ≠ 抄了逻辑。剥掉「改一字就破坏线上协议」的部分后，真正可自由改写的逻辑重合度降到 0.28–0.36；唯一例外 `hass_play_music`，非契约内容仅约 30 token，属结构性地板而非未重写。

## 6. 附录：工具链主体（已达标，审计阈值外）

15 个 `providers/tools/` 主体文件重写后 **LOGIC 0.070–0.388 全部达标**，合计 **148 条门断言全绿**（mcp_endpoint 49/49 + server_mcp 80/80 + device_iot 19/19）。详见 `behavior-gate.md`。

---

## 7. 两轮误判（数字反咬的教训，详见 methodology §3）

- **误判一**：`providers/tools/` 9 件原误判为 🔴 真派生·逐字 → 实为 R 契约锁定豁免（1 件 0 行空文件、6 件纯导出壳、2 件枚举/ABC），工具链主体重写后 LOGIC **0.07–0.39**。
- **误判二**：`hass_*` 4 件原误判为 🟡 复果自有·巧合相似（不重写）→ 实测 `contract_floor` 剥契约后 LOGIC-LOCK 仍 **0.515/0.547/0.774/0.807**，且 fork-start 与 main 双基线相同（fork 时即上游原样）→ 真派生，已全部重写（0.494→0.126 / 0.605→0.438 / 0.853→0.471 / 0.820→0.583）+ 门 61/61。

> **结论**：本仓应用源码已逐文件给出「派生 / 重写 / 契约锁定」判据与可复核证据链，缺口 0。许可层面详见 `LICENSE` 与 `audit/THIRD_PARTY_LICENSES.md`（MIT 派生渊源已如实披露并保留原声明）。

---

## 8. 全集映射：此前未入表的 59 文件（2026-11-01 补齐）

> 背景：`scan_derivation_v2.py` 全量实跑发现 122 个上游同名文件，其中 59 个不在 §3 主表 / §4 附录内。逐一回源定性后映射完毕：**52 个阈值达标 C + 1 个真漏网已重写（get_time）+ 6 个基名碰撞假阳性 N**。判据分工：
> - **C（阈值达标）**：LOGIC<0.40，按 §1 口径即为重写达标；有行为门者另注门证。
> - **N（复果自有·假阳性）**：上游**无同路径**文件，scan 按基名匹配撞上的跨路径噪声（`__init__.py` / `native/*` / `embedding/*`），逐一经同路径核验排除。
> - **X（真派生）**：0——唯一 ≥0.40 漏网 `get_time.py`（0.417，同路径真派生候选）已按 campaign 硬线重写。

### 8.1 真漏网重写件（1）

| 文件 | 重写前 LOGIC | 重写后 LOGIC | 重写方式 | 行为证据 |
|------|------------:|------------:|------|------|
| `plugins/functions/get_time.py` | 0.417 | **0.327** | 2026-11-01 clean-room：巨型 `%` 拼接重构为「字段表 `_LUNAR_FIELDS` + 统一渲染 `_render_lunar_response`」；schema（LLM 工具契约）与响应文本**逐字节不变** | 3 个固定日期旧新输出比对 IDENTICAL + schema dict 相等；新增 `audit/get_time_gate.py` 21 断言全绿（注册名/ToolType/日期容错/缓存命中/UTC+8/21 行渲染结构） |

### 8.2 基名碰撞假阳性 → N 复果自有（6，同路径核验排除）

| 文件 | LOGIC | 上游同路径 | 定性依据 |
|------|------:|:---:|------|
| `protocol/falp/__init__.py` | 0.420 | 无 | FALP v1 复果自有通信协议（docstring 自证 clean-room、与 xiaozhi 线格式零兼容）；0.420 系与 `tools/base/__init__.py` 的 `__init__.py` 基名碰撞（FULL 仅 0.066） |
| `native/output_counter.py` | 0.319 | 无（无 `core/native/`） | 复果原生模块；匹配自上游 `utils/output_counter.py` 基名 |
| `providers/embedding/base.py` | 0.133 | 无（无 `core/providers/embedding/`） | 复果自有 provider |
| `native/auth.py` | 0.128 | 无 | 复果原生模块 |
| `native/p3.py` | 0.047 | 无 | 复果原生模块 |
| `providers/embedding/__init__.py` | 0.045 | 无 | 复果自有 provider |

### 8.3 阈值达标 C（52，同路径真派生候选、LOGIC<0.40）

| # | 文件 | LOGIC | 门证 |
|---|------|------:|------|
| 1 | `utils/auth.py` | 0.396 | — |
| 2 | `plugins/functions/get_news_from_chinanews.py` | 0.390 | — |
| 3 | `providers/tools/device_iot/iot_executor.py` | 0.386 | device_iot_gate |
| 4 | `providers/intent/intent_llm/intent_llm.py` | 0.384 | — |
| 5 | `api/base_handler.py` | 0.381 | — |
| 6 | `utils/cache/config.py` | 0.379 | — |
| 7 | `utils/intent.py` | 0.369 | — |
| 8 | `auth.py` | 0.361 | — |
| 9 | `handle/textHandler/listenMessageHandler.py` | 0.361 | listenMessageHandler_gate |
| 10 | `handle/receiveAudioHandle.py` | 0.358 | — |
| 11 | `providers/tools/device_mcp/mcp_executor.py` | 0.357 | device_mcp_gate |
| 12 | `providers/tools/mcp_endpoint/mcp_endpoint_executor.py` | 0.339 | mcp_endpoint_gate |
| 13 | `utils/prompt_manager.py` | 0.338 | — |
| 14 | `providers/asr/base.py` | 0.338 | asr_base_gate |
| 15 | `providers/tools/server_mcp/mcp_executor.py` | 0.330 | server_mcp_gate |
| 16 | `handle/helloHandle.py` | 0.327 | — |
| 17 | `providers/tts/edge.py` | 0.326 | — |
| 18 | `app.py` | 0.322 | — |
| 19 | `providers/tools/mcp_endpoint/mcp_endpoint_handler.py` | 0.321 | mcp_endpoint_gate |
| 20 | `handle/abortHandle.py` | 0.315 | — |
| 21 | `utils/audioRateController.py` | 0.310 | audioRateController_gate |
| 22 | `providers/tools/server_mcp/mcp_manager.py` | 0.305 | server_mcp_gate |
| 23 | `providers/tools/device_mcp/mcp_client.py` | 0.303 | device_mcp_gate |
| 24 | `utils/context_provider.py` | 0.293 | — |
| 25 | `plugins/functions/get_weather.py` | 0.281 | — |
| 26 | `handle/textHandler/serverMessageHandler.py` | 0.277 | serverMessageHandler_gate |
| 27 | `handle/textMessageType.py` | 0.272 | textMessageType_gate |
| 28 | `providers/memory/mem_local_short/mem_local_short.py` | 0.271 | mem_local_short_gate |
| 29 | `websocket_server.py` | 0.265 | — |
| 30 | `handle/reportHandle.py` | 0.259 | — |
| 31 | `handle/textMessageHandlerRegistry.py` | 0.259 | textMessageHandlerRegistry_gate |
| 32 | `api/ota_handler.py` | 0.248 | — |
| 33 | `providers/tools/unified_tool_manager.py` | 0.207 | — |
| 34 | `providers/tools/device_iot/iot_handler.py` | 0.207 | device_iot_gate |
| 35 | `utils/memory.py` | 0.202 | — |
| 36 | `providers/tools/device_mcp/mcp_handler.py` | 0.199 | device_mcp_gate |
| 37 | `providers/tools/unified_tool_handler.py` | 0.197 | — |
| 38 | `providers/tools/server_mcp/mcp_client.py` | 0.193 | server_mcp_gate |
| 39 | `providers/tts/tencent.py` | 0.190 | — |
| 40 | `utils/util.py` | 0.188 | — |
| 41 | `providers/tools/mcp_endpoint/mcp_endpoint_client.py` | 0.186 | mcp_endpoint_gate |
| 42 | `providers/tools/device_iot/iot_descriptor.py` | 0.170 | device_iot_gate |
| 43 | `providers/vllm/openai.py` | 0.157 | vllm_openai_gate |
| 44 | `utils/modules_initialize.py` | 0.150 | modules_initialize_gate |
| 45 | `http_server.py` | 0.143 | — |
| 46 | `providers/llm/base.py` | 0.137 | — |
| 47 | `utils/wakeup_word.py` | 0.134 | — |
| 48 | `fuguoai_config/config_loader.py` | 0.098 | — |
| 49 | `utils/voiceprint_provider.py` | 0.095 | — |
| 50 | `utils/cache/manager.py` | 0.090 | — |
| 51 | `providers/tools/server_plugins/plugin_executor.py` | 0.069 | — |
| 52 | `utils/p3.py` | 0.047 | — |

> **口径**：路径均省略包前缀（`fuguoai_core/`、`fuguoai_plugins/`、`fuguoai_config/` 除外者已写全）；"门证 —"= 暂无独立行为门，达标依据为 §1 阈值口径（LOGIC<0.40 = C 档）。数字出自 `scan_derivation_v2.py` 2026-11-01 实跑（`[硬·实测]`）。
>
> **全集合计 122**：§3 主表 58（C/R/F）+ §4 附录 5（C）+ 本节 59（52C + 1C重写 + 6N）。X=0、缺口=0。
