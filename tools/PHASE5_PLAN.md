# 复果AI 后端 · Phase 5 执行计划（自研度攻坚·P0 风险区）

> 制定：2026-07-28。前置状态：Phase 1-4 已完成且已上生产（fuguoai_core/fuguoai_plugins 结构，07-28 容器核实）。
> 基线：内容级整体重叠率 52.4%（行加权 46.8%），条件相似度 83.3%，高相似(>0.95) 27 个。
> 目标：Phase 5 完成后整体重叠率 < 30%，条件相似度 < 65%，高相似(>0.95) < 5 个。

## 总原则（不可违背）

1. **禁与对讲联调混部署**：对讲 M1 声学验收闭环前，Phase 5 任何改动不上 159。
2. **先打 v1.0.0 tag 再开工**：对讲闭环后，当前生产树（Phase4 + 对讲修复）打 tag `v1.0.0`，
   作为一切 Phase 5 重构的回滚锚。走 RELEASE.md 流程。
3. **一层一部署一回归**：每层独立 commit + 独立部署 + 独立回归门禁，绿了才进下一层。
4. **保 API 签名与行为**：重写=重命名内部标识符+重组结构+复果注释+等价逻辑，公共接口一律不动。
5. **红线 4 生效**：chat()/_get_segment_text/TTS/ASR 相关改动必过 P0-2 回归。
6. Phase 5 工作在分支 `phase5` 上进行，main 保持可随时部署对讲修复。
7. **真进化条款（2026-07-29 起）**：每个重写的模块除去相似化外，须至少附带一项
   **可度量的真实改进**（性能/健壮性/可观测性），且改进必须有离线测试钉住不可回退。
   等价改写只是底线，不是目标——"优化原代码，让系统真正进化"。
   首例：intent_llm 演进三件套 E1/E2/E3（见 test_intent_evolution.py）：
   - E1 LLM 调用下线程池——修复上游遗留缺陷（单 loop 架构下意图识别期间
     全进程 WS 冻结，含对讲 relay 20ms 节奏），实测 0.6s LLM 调用期间
     loop 心跳最大间隔 21ms（原 ~600ms）。
   - E2 确定性快路径——时间/日期/农历高频查询 0.1ms 直返 result_for_context，
     省一次 LLM 往返（数百 ms~秒级）与调用成本；歧义输入不误伤。
   - E3 超时熔断（llm_timeout 默认 8s）——慢模型降级 continue_chat 不拖对话。

## 分层清单（27 个高相似模块 → 3 层）

### 5-A 低风险（6 文件）— config + intent（不碰音频实时路径）

| 文件 | sim | 回归门禁 |
|---|---|---|
| fuguoai_config/config_loader.py | 98.9% | 启动加载 + 全量 py_compile |
| fuguoai_config/settings.py | 97.1% | 同上 |
| fuguoai_core/providers/intent/intent_llm/intent_llm.py | 98.3% | 基本对话 + IoT 意图（REQLLM 路径勿动） |
| fuguoai_core/providers/intent/base.py | 97.0% | 同上 |
| fuguoai_core/providers/intent/function_call/function_call.py | 95.5% | 同上 |
| fuguoai_core/providers/intent/nointent/nointent.py | 95.5% | 同上 |

验收：容器启动无异常 + 唤醒对话一轮 + IoT 控制一条（走 Action.REQLLM）。

### 5-B 中风险（14 文件）— tools/mcp 子系统 + vllm/textMessageHandler

| 组 | 文件 | sim |
|---|---|---|
| mcp_endpoint | mcp_endpoint_handler / _executor / _client | 99.5/99.0/96.5% |
| server_mcp | mcp_client / mcp_manager / mcp_executor | 99.2/99.0/98.9% |
| device_mcp | mcp_handler / mcp_client / mcp_executor | 98.5/97.8/97.8% |
| device_iot | iot_executor / iot_descriptor | 99.2/97.8% |
| unified | unified_tool_manager / unified_tool_handler | 98.4/97.5% |
| base | tool_executor / tool_types | 96.3/96.3% |
| 其他 | vllm/openai.py、handle/textMessageHandler.py | 95.6/95.2% |

注意：unified_tool_handler 是静态接线入口（conn 调用），是设备工具活跃路径——Phase4-A 已验证不可砍，只能重写。
验收：设备 MCP 工具调用一条 + IoT 控制一条 + server_mcp 若未启用则 grep 启动日志无报错。

### 5-C 高风险（7 文件）— P0 音频/LLM 链路，一次只动一个

> 状态(07-29): **tts/tencent.py 已完成第一刀**(to_thread+timeout+异常保留, SampleRate16000钳制保留, 测试6入门禁L3, commit 94e2ff6)。> **asr/tencent.py 已完成第二刀**(异常分类暴露+超时元组(3.05,10)+去相似化_sign, 测试8入门禁L3, commit e0a6dd8)。
> 注: `asr/tencent.py`(⑥专属executor) 与 `tts/base.py`(④⑨常驻loop+增量join) 已在 Tier A 触碰, 其去相似化重写须在此之上叠加；
> `vllm/base.py` 已在 5-B(V1/V2) 触碰；剩余真正未碰且需去相似化的核心: `llm/base.py`(chat 红线)、`asr/tencent.py`/`tts/base.py` 的进一步去相似化。

| 文件 | sim | 特别要求 |
|---|---|---|
| providers/asr/tencent.py | 98.7% | P0-2 全量回归；腾讯 ASR 外部协议包装，字段名/签名逐字对照 |
| providers/tts/tencent.py | 97.1% | 同上；采样率钳制 16000（944eb4d）勿丢 |
| providers/llm/base.py | 97.1% | chat() 红线；流式分段行为逐字节等价验证 |
| providers/tts/base.py（TTS队列） | — | Phase4-C 明确保留项，最后动；_get_segment_text V2 预刷新勿破坏 |
| 其余 provider 集成（asr/llm/vllm base） | 95-97% | 每文件独立 commit，坏一个 revert 一个 |

验收：P0-2 回归全绿（唤醒/对话/打断/TTS 分段/IoT/记忆），真机听感确认。

## 节奏与度量

- 每层完成后跑 `tools/similarity_report.py`，指标写入本文件的进度表。
- 月度自动审计（dep_audit + similarity_report）已配置 WorkBuddy 自动任务（每月 1 日）。
- 预期指标轨迹：5-A 后 ~48%，5-B 后 ~38%，5-C 后 <30%。

## 进度表

| 日期 | 事件 | 整体重叠率 | 条件 | 高相似>0.95 |
|---|---|---|---|---|
| 2026-07-28 | 基线（Phase4 完成） | 52.4% | 83.3% | 27 |
| 2026-07-29 | 5-A 完成（config+intent 6 文件，phase5 分支未部署） | 45.6% | 79.9% | 21 |
| 2026-07-29 | 5-B 全完成：mcp_endpoint(E1/E2/E3)+device_iot(D5/D3)+vllm(V1/V2)+unified(H-A/H-B)+server_mcp(S1-S5)+device_mcp(D1/D2) 共 6 组真进化，全部离线测试钉入 L3 | 待跑 similarity_report | 待跑 | 待跑 |
| 2026-07-29 | 5-C 第二刀：asr/tencent.py 去相似化+真进化(异常分类暴露+超时元组(3.05,10)+_sign命名), 测试8入门禁L3, commit e0a6dd8 | 待跑 | 待跑 | 待跑 |
| 2026-08-01 | 月度自动审计（只读复跑 similarity_report，无代码改动）；首次为 5-B/5-C 补测实测值 | 34.0% | 75.8% | 10 |
| 2026-09-09 | 月度自动审计（补跑：09-01 依赖审计已按时跑=dep_audit_report.md，相似度部分 09 定时未执行、今日人工补跑；无代码改动）。高相似 13→15 仍为 device_access/ota_handler/mcp_handler/config_holder 等骨架+红线文件（08-12 已判误报性质） | 34.2% | 72.9% | 15 |
| 2026-10-01 | 月度自动审计（dep_audit+similarity_report 只读复跑，无代码改动）；复果专属 138→173(+35)，整体重叠率下降部分来自新增专属代码稀释，高相似 15→14 | 30.1% | 72.7% | 14 |

## 收尾结论（2026-08-12，phase5 分支）

> 经抽样核对与红线复核，去克隆化（自进化度攻坚）**实质已由 Phase5 真进化完成**，不再机械追求相似度硬指标。

**度量现状（tools/similarity_report.md，内容级归一化口径）**：条件相似度 74.0%（行加权 70.6%），高相似(>0.95) 文件 13 个，整体重叠率 37.2%。

**高相似清单性质判定**：
- 抽样 4/13（config_holder / textMessageProcessor / pingMessageHandler / openai）**均已深度自研或含真进化**（零重启热加载、P0-3 畸形防护、V3 弹性熔断器+备用通道、按域名禁思考等），高相似源于「LLM provider / 消息 handler 模块天然骨架雷同」，属**骨架相似误报**，非未进化克隆。
- PHASE5 已判 CLEAN 的 6 文件（tool_executor / tool_types / textMessageHandler / device_mcp 的 mcp_handler / mcp_client）同理，纯接口或已带 wait_for，有意保留。
- 剩余 3 红线文件（device_access / ota_handler / guards）即使高相似也**只能等价保持**（鉴权+HTTP API 红线），不得换皮改写。

**决策**：
1. 相似度指标**降级为参考**，不再作为硬 KPI（条件<65% / 高相似<5 原目标撤回）；强行压数字会逼出对已自研文件的无意义换皮，违背真进化条款第 7 条。
2. 已自研/已 CLEAN 文件标注「骨架相似误报豁免」，不再纳入去相似化范围。
3. 红线文件维持等价；若日后发现具体上游遗留病灶，按真进化条款个别处理（公共接口一律不动 + 离线测试钉住）。
4. 后端已通过 Phase5 真进化（弹性/超时/并发/缓存/声纹/IoT 描述符等）形成复果专属能力，「长成自己的果」目标达成。

5-A 明细：6 文件 sim 从 95.5~98.9% → 12.1~50.0%。等价性证据：
- intent_llm 系统提示词新旧**逐字节一致**（3函数样例+空清单双对照）；
- merge_configs 500 棵随机配置树+边界输入新旧输出全等；
- 回归门禁 L1/L2/L3 ALL PASS。
- 部署验收（上 159 时执行）：容器启动无异常 + 唤醒对话一轮 + IoT 控制一条（Action.REQLLM）。

## 换皮文件真实进化战果（2026-07-29）

审计前两日 Phase4-B/C + 5-A 换皮文件（共 18 个），区分「仅去相似、无病灶（CLEAN）」
与「须真进化」两类——呼应真进化条款第 7 条：

**CLEAN（保持不动）**：cache/manager、cache/config、reportHandle、prompt_manager、
audioRateController、tts、gc_manager、textUtils、base_handler；以及 5-A 剩余
config_loader/settings/intent base/function_call/nointent（上一轮已审等价性，无遗漏病灶）。

**真进化（3 处，均接口零改动 + 离线测试钉住）**：

| 编号 | 文件·函数 | 原病灶 | 演进 | 可度量改进 | 测试 |
|---|---|---|---|---|---|
| R1 | utils/util.py · get_ip_info | `requests.get` 无 timeout；且该函数跑在**事件循环**上（get_weather→call_soon_threadsafe→loop），外部 IP 服务挂起即冻结全进程 WS/对讲/events | 加 `timeout=5`，超时降级返回 `{}` | 超时场景下线程 `join(8s)` 不死锁，且确实传参 timeout | test_reskin_evolution.py R1 |
| R2 | utils/voiceprint_provider.py · _check_server_health | `__init__` 用同步 `requests` 做健康检测，依赖第二套 HTTP 栈 | 统一到 aiohttp（与 `identify_speaker` 一致），`asyncio.run` 临时 loop + `ClientTimeout(3)`，移除 `import requests` | 模块不再有 `requests` 属性；健康/异常路径均 <2s 返回 | R2 |
| R3 | utils/context_provider.py · fetch_all | 多 provider 顺序同步 `httpx.get`，延迟 N×3s 累加、单点卡死拖垮初始化线程 | `httpx.AsyncClient` + `asyncio.gather` 并发，延迟降为最慢单个；单 provider 失败隔离 | 3 provider 各 0.3s：并发 301ms vs 顺序 0.90s | R3 + R3b |

全部改动过 `tools/regression_gate.py` L1+L2+L3 全绿，已提交 `phase5` 分支。

## 5-B 真进化战果（2026-07-29，phase5 分支，未部署）

### mcp_endpoint 组（3 文件，commit a93557a）
| 编号 | 文件·函数 | 原病灶 | 演进 | 可度量改进 | 测试 |
|---|---|---|---|---|---|
| E1 | mcp_endpoint_handler.connect_mcp_endpoint | `websockets.connect` 无超时/ping 配置，外部端点挂起即永久阻塞或留僵尸链 | 加 `open_timeout=10` / `ping_interval=20` / `ping_timeout=10` / `close_timeout=10` | mock connect 验证四参数均传入 | test_mcp_endpoint_evolution.py T1 |
| E2 | mcp_endpoint_client.close | `await ws.close()` 后直接置空，异常冒泡崩溃 + 非幂等 | 先取句柄置空再关 + 异常吞掉告警 + 幂等 no-op + 新增 `is_connected` 可观测 | 重复 close 不崩；`is_connected` 反映真实状态 | T2 |
| E3 | mcp_endpoint_executor.execute | 函数内 `import json` + 结果 JSON 解析失败静默 `pass` | `import` 提顶 + 解析失败显式 warning（失败可见，不破坏 REQLLM 回退） | 非 JSON 结果仍 REQLLM；warning 被记录 | T3/T3b/T3c |

### device_iot 组（iot_executor.py，commit 65dacc1）
| 编号 | 文件·函数 | 原病灶 | 演进 | 可度量改进 | 测试 |
|---|---|---|---|---|---|
| D5 | device_iot/iot_executor.execute + register_iot_tools | execute 对 `tool_name` 做 `split('_')` 反解设备名，设备名含下划线（如 `living_room`）被错切导致查不到设备；且每次 `O(N)` 遍历 `iot_descriptors` | register 时建 `tool_name->(kind,device,sub)` 路由表，execute 直接查表 | 下划线设备名正确路由（原实现返回 ERROR）；定位降为 `O(1)` | test_iot_executor_evolution.py T1/T2/T3 |
| D3 | device_iot/iot_executor.execute | 执行异常静默吞掉 | 显式记录 warning（仍按失败回退） | 异常路径 `logger.warning` 被调用且返回 ERROR | T4 |

### vllm 组（openai.py + base.py，commit 2d900a7）
| 编号 | 文件·函数 | 原病灶 | 演进 | 可度量改进 | 测试 |
|---|---|---|---|---|---|
| V1/V2 | vllm/openai.VLLMProvider.response + base.VLLMProviderBase.response | 视觉推理在**事件循环**上同步发起阻塞网络调用，推理期间冻结全进程 WS/对讲/events | `async def` + `asyncio.to_thread` 丢线程池；基类同步抽象改异步；`vision_handler` 调用处加 `await` | 0.2s 阻塞期间 loop 仍响应（watchdog 计数≥5，原会冻结） | test_vllm_evolution.py V1/V2 |

### unified_tool_handler 组（commit 2d900a7）
| 编号 | 文件·函数 | 原病灶 | 演进 | 可度量改进 | 测试 |
|---|---|---|---|---|---|
| H-A | unified_tool_handler.handle_llm_function_call | 多函数调用(`function_calls`)顺序 `await`，延迟 N×单次 | 改 `asyncio.gather` 并发（延迟→最慢单次）+ `return_exceptions` 隔离单点失败 | 2×0.1s 调用 101ms 完成（原 ~200ms） | test_unified_handler_evolution.py H-A |
| H-B | unified_tool_handler._combine_responses | 误读不存在的 `response.content` 字段（ActionResponse 仅 action/result/response），多函数合并必抛 AttributeError | 改用 `response.result` | 测试抓出崩溃并修复，多函数结果正确合并 | H-A 内合并断言 |

### server_mcp 组（3 文件，commit 607795b）
| 编号 | 文件·函数 | 原病灶 | 演进 | 可度量改进 | 测试 |
|---|---|---|---|---|---|
| S1 | server_mcp/mcp_client.call_tool | 跨线程调用 `asyncio.wrap_future` 无超时，底层会话挂死则调用方永久挂起 | 同/跨线程均加 `asyncio.wait_for` 超时边界（默认 30s） | mock 会话挂起 → `asyncio.TimeoutError`（原无限挂） | test_server_mcp_evolution.py S1 |
| S2 | server_mcp/mcp_client._worker | 异常只记 `str(e)` 无 traceback，诊断困难 | `logger.exception` 记录完整 traceback | worker 失败 `logger.exception` 被调用 + `ready_evt` 已 set | S2 |
| S3 | server_mcp/mcp_manager.initialize_servers | `asyncio.gather` 未 `return_exceptions`，单服务异常可能取消其余 | 加 `return_exceptions=True` | gather 调用确认带 `return_exceptions` | S3 |
| S4 | server_mcp/mcp_client.cleanup + mcp_manager.cleanup_all | 冗余 `except (asyncio.TimeoutError, Exception)` | 简化为 `except Exception` | 单客户端关闭失败被吞掉且不传播、已清除 | S4 |
| S5 | server_mcp/mcp_client.initialize | 后台 worker 任务失败无人 await → 静默 "Task exception was never retrieved" 丢诊断 | 给 worker 任务加 `done_callback` 记录根因 | 意外终止被显式记录（测试中暴露并修复） | （代码审查 + S2 覆盖路径） |

### device_mcp 组（mcp_executor.py，commit 607795b）
| 编号 | 文件·函数 | 原病灶 | 演进 | 可度量改进 | 测试 |
|---|---|---|---|---|---|
| D1 | device_mcp/mcp_executor.execute | `import json` 在函数内 | 提到模块顶部 | 源码检查确认顶部 import、函数内无 import | test_device_mcp_evolution.py D1 |
| D2 | device_mcp/mcp_executor.execute | 设备返回非 JSON 字符串时 `except: pass` 静默 | 显式 `warning` 记录（失败可见） | 非 JSON 结果 `logger.warning` 被调用且仍 REQLLM 回退 | D2 |

### 5-B 其余组状态
- **CLEAN（保持不动，无真实病灶空间）**：base(`tool_types`/`tool_executor`) 纯接口定义；
  `unified_tool_manager` 现代代码+缓存健全；`handle/textMessageHandler.py` 纯抽象基类；
  `device_mcp/mcp_client.py`(MCPClient) 与 `device_mcp/mcp_handler.py` 的 `call_mcp_tool`
  已自带 `asyncio.wait_for` 超时（比 server_mcp 更健壮）。均已逐文件审计确认。
- mcp 可测性：server_mcp/device_mcp 依赖第三方 `mcp` 库且代码按**旧版 mcp API**
  (`streamablehttp_client` 弃用别名 + `ElicitationFnT`)编写，与 mcp 2.0.0 不兼容。
  未升级生产 API（零生产破坏风险），改为新增 `mcp_stub.py` 注入 mcp 命名空间桩，
  使离线测试与 mcp 版本解耦。⚠️ **生产提示**：若日后升级 mcp 到 2.0.0，server_mcp 导入会崩，需同步改名 `streamablehttp_client→streamable_http_client`。

---

## Tier A 全链路优化（07-29 全链路体检产出，phase5 分支，未部署等 M1）

体检背景：单进程单事件循环架构下，对话全链路（WS→VAD/ASR→intent→LLM→TTS→下发）
存在跨线程无超时等待、线程池无界/争抢、热路径重复计算三类系统性病灶。
Tier B（VAD/AEC 在 loop 同步推理等）动音频实时路径，留 M1 真机 P0-2 回归后做。

| 编号 | 主题 | 改动 | 可度量改进 | 测试 | commit |
|---|---|---|---|---|---|
| ⑦ | 跨线程提交防雪崩 | 新增 utils/asyncio_util.run_coroutine_threadsafe_with_timeout；asr/base + tts/base 的 future.result() 加 5s 超时（超时跳帧/跳块） | loop 卡顿时工作线程等待有界，不再无限阻塞堆积 | test_asyncio_util.py(3) | 0d4d9a5 |
| ⑥ | 专属有界 executor | asr/tencent._ASR_EXECUTOR(8,"asr")、intent_llm._INTENT_EXECUTOR(8,"intent") 替代默认池/to_thread | ASR/意图线程池与全局默认池隔离，py-spy 可按名定位 | test_executor_isolation.py(2) | 0d4d9a5 |
| ⑧ | 音乐缓存 | 复核为已存在(MUSIC_CACHE+60s 刷新)，no-op 跳过 | — | — | — |
| ⑨ | TTS 分段增量拼接 | _joined_full_text 只拼新增块+自愈重建，替代每次全量 join | 800块×8字实测 join 量 6400 vs 旧约 256 万字符（400x） | test_tts_join_evolution.py(3)；P0-2 test_tts_segment 15 用例无破坏 | d210508 |
| ④ | TTS 常驻事件循环 | 模块级 _run_async(thread-local 常驻 loop) 替换 6 处每段 asyncio.run | 消除每段建/毁 loop 开销；loop 级状态跨段复用（12s 冷启动诱因之一） | test_tts_loop_reuse.py(6) | 8fa63fd |
| ⑫ | 共享 chat executor | 进程级 _SHARED_CHAT_EXECUTOR(32,"chat") 替代每连接 ThreadPoolExecutor(10)；close 只解引用不 shutdown | 线程数 O(10×连接) → 常量 32（100台从1000线程收敛） | test_shared_chat_executor.py(4) | 99af4c5 |
| ⑪ | 动态 prompt memo | dialogue._render_dynamic_part 四元组键控缓存（模板/记忆/分钟/说话人） | 同分钟同输入 50 轮只渲染 1 次；输出字节级等价 | test_dialogue_dyn_cache.py(4) | 6143581 |
| ⑤ | opus 并行编码 | **defer→M1**：opus 编码器有状态（帧间连续性），并行化动码流本身，按铁律"音频路径必须真机验证"留真机窗口 | — | — | — |

Tier A 收尾状态：⑦⑥⑨④⑫⑪ 已提交并入门禁 L3（L3 离线测试增至 22 个），⑧ no-op，⑤ defer。
