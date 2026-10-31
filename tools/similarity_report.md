# 复果AI 后端 · 与参考基线 相似度报告

> 自动生成（只读扫描，未改动任何代码）。ours=`fuguoai-server`，ref=`/Users/xiongxiong/WorkBuddy/2026-07-09-21-43-20/_reference_snapshot/self/fork-start/fuguoai-server`
> 参考基线：**本仓 fork 起点(8f051a8) 自有历史快照**（可用 FUGUOAI_REF_SUB 环境变量手动覆盖；缺失时自动从 git 重建本仓 fork 起点快照，不回退上游）

## 摘要指标（相对路径对齐口径）

- 共同 .py 文件：**1**
- 一字未改（相似度=1.0）：**0** (0.0%)
- 复果专属（上游无对应）：**353**
- 文件级平均相似度（条件·仅路径对齐文件）：60.6%
- 行加权相似度（条件·仅路径对齐文件）：60.6%
- 文件级整体重叠率（含复果专属=0）：0.2%
- 行加权整体重叠率（含复果专属=0）：0.3%

## 关键：内容级归一化相似度（剥 `fuguoai_` 前缀后配对上游）

> 相对路径口径因包名 fuguoai_core↔core 不一致而几乎不重叠，严重低估真实差异。
> 此段剥掉 `fuguoai_` 前缀再双侧配对，才是真实的内容自研度；自进化进度以此段为准。

- 内容级共同 .py 文件：**122**
- 内容级一字未改：**1**
- 内容级复果专属：**232**
- **内容级文件级平均相似度（条件）：56.2%**
- **内容级行加权相似度（条件）：57.1%**
- **内容级文件级整体重叠率（含复果专属=0）：19.4%**
- **内容级行加权整体重叠率（含复果专属=0）：19.2%**

### 内容级几乎未改的高相似模块（>0.95，重构候选）

- （无）

### 内容级已明显自研的模块（<0.7，已是复果设计）

- `core/device_access.py`  sim=1.5%
- `core/utils/voiceprint_provider.py`  sim=5.0%
- `core/utils/config_holder.py`  sim=5.6%
- `core/providers/llm/base.py`  sim=9.1%
- `core/providers/intent/intent_llm/intent_llm.py`  sim=9.1%
- `core/utils/device_store.py`  sim=9.2%
- `core/utils/event_bus.py`  sim=9.8%
- `core/guards.py`  sim=13.2%
- `core/utils/util.py`  sim=14.8%
- `core/utils/cache/manager.py`  sim=14.8%
- `core/api/ota_handler.py`  sim=17.3%
- `core/utils/p3.py`  sim=17.5%
- `core/utils/usage_store.py`  sim=19.3%
- `core/providers/tools/device_iot/iot_descriptor.py`  sim=20.7%
- `core/utils/cache/config.py`  sim=20.9%
- `config/config_loader.py`  sim=21.0%
- `core/providers/tools/server_plugins/plugin_executor.py`  sim=21.5%
- `core/utils/context_provider.py`  sim=23.2%
- `core/handle/textMessageProcessor.py`  sim=26.6%
- `core/providers/tts/tencent.py`  sim=27.5%
- `core/utils/prompt_manager.py`  sim=28.6%
- `core/auth.py`  sim=31.2%
- `core/utils/output_counter.py`  sim=32.7%
- `core/utils/wakeup_word.py`  sim=32.8%
- `core/providers/tts/dto/dto.py`  sim=33.7%
- `core/utils/auth.py`  sim=35.1%
- `core/providers/tools/server_mcp/mcp_manager.py`  sim=35.3%
- `core/providers/vad/base.py`  sim=36.4%
- `core/handle/receiveAudioHandle.py`  sim=37.0%
- `core/providers/tools/server_mcp/mcp_executor.py`  sim=37.9%
- `core/providers/tools/server_mcp/mcp_client.py`  sim=38.6%
- `core/handle/helloHandle.py`  sim=38.9%
- `core/handle/abortHandle.py`  sim=39.1%
- `core/utils/opus_encoder_utils.py`  sim=39.4%
- `core/handle/textHandler/listenMessageHandler.py`  sim=39.4%
- `core/providers/asr/dto/dto.py`  sim=40.0%
- `core/providers/tools/mcp_endpoint/mcp_endpoint_handler.py`  sim=41.4%
- `core/utils/agent_config_store.py`  sim=43.2%
- `core/handle/textHandler/abortMessageHandler.py`  sim=43.6%
- `core/api/events_routes.py`  sim=45.6%
- `core/handle/textHandler/iotMessageHandler.py`  sim=45.7%
- `core/providers/intent/function_call/function_call.py`  sim=45.8%
- `core/providers/tts/edge.py`  sim=46.3%
- `core/providers/intent/nointent/nointent.py`  sim=46.8%
- `config/settings.py`  sim=47.4%
- `core/providers/tools/mcp_endpoint/mcp_endpoint_executor.py`  sim=47.5%
- `core/websocket_server.py`  sim=48.3%
- `core/utils/textUtils.py`  sim=49.0%
- `core/providers/tools/device_iot/iot_handler.py`  sim=49.0%
- `core/providers/intent/base.py`  sim=50.0%
- `core/providers/tools/base/tool_types.py`  sim=50.0%
- `core/utils/tts.py`  sim=50.5%
- `core/utils/bind_token_store.py`  sim=52.1%
- `core/utils/agent_config_overlay.py`  sim=52.2%
- `core/utils/circuit_breaker.py`  sim=52.5%
- `core/handle/textHandler/pingMessageHandler.py`  sim=54.3%
- `core/providers/tools/unified_tool_manager.py`  sim=54.7%
- `core/utils/user_auth.py`  sim=55.2%
- `core/providers/tools/device_mcp/mcp_handler.py`  sim=55.6%
- `core/providers/memory/base.py`  sim=57.5%
- `core/handle/textMessageHandler.py`  sim=59.1%
- `core/utils/gc_manager.py`  sim=59.6%
- `core/providers/tools/device_iot/iot_executor.py`  sim=59.9%
- `app.py`  sim=60.6%
- `core/providers/tools/base/tool_executor.py`  sim=60.7%
- `core/utils/llm.py`  sim=61.3%
- `core/utils/vllm.py`  sim=61.5%
- `core/providers/memory/mem_report_only/mem_report_only.py`  sim=62.2%
- `core/handle/sendAudioHandle.py`  sim=63.1%
- `core/handle/reportHandle.py`  sim=63.3%
- `core/utils/audioRateController.py`  sim=63.4%
- `core/providers/memory/nomem/nomem.py`  sim=63.6%
- `core/api/base_handler.py`  sim=64.3%
- `core/api/devices_routes.py`  sim=66.8%
- `core/providers/tools/device_mcp/mcp_client.py`  sim=67.7%
- `core/providers/tts/default.py`  sim=67.9%
- `core/connection.py`  sim=69.4%
- `core/api/vision_handler.py`  sim=69.4%
- `core/providers/tts/base.py`  sim=69.4%

### 内容级一字未改文件全清单（按归一化路径）

- `core/providers/tools/__init__.py`

## 相对路径口径附属清单

### 疑似未使用的一字未改文件（删前需 grep 复核配置名/字符串引用）

- （无）

### 相对路径高相似模块（>0.95）

- （无）

### 相对路径一字未改清单


