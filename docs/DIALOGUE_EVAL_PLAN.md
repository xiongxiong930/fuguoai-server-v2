# 对话自评估 + A/B + 离线兜底（创新维度④）

> 状态：已实现 + 离线测试全绿 + 待 159 部署验证
> 分支：`phase5`（自进化主线延续）
> 关联：维度②长期记忆+人格、维度③个性化语音克隆、维度⑤家庭RAG 为后续方向

## 0. 动机（为什么是真进化，而非功能补全）

复果AI 此前在"回答质量"与"失败兜底"上有两个真实缺口：

1. **零可观测性**：运营/研发无法知道某台设备、某个实验桶的回答质量是否退化——没有指标、没有评分。
2. **死寂兜底**：`connection.py` 在 LLM 主+备双通道全失败时 `return None`，用户面对的是**完全静音**，没有任何 TTS 或错误提示。

本方案同时补上"可度量质量信号"（judge + 指标）、"可控实验基础设施"（A/B 分桶）、"永不死寂的兜底"（offline fallback）。

## 1. 设计总览

| 模块 | 文件 | 职责 | 真进化改进（可度量） |
|---|---|---|---|
| 离线兜底 | `utils/offline_fallback.py` | 纯函数：返回兜底文本（优先级 answer > system_error_response > 内置） | 消除"死寂"；纯函数可离线断言 |
| 对话自评估 | `utils/dialogue_eval.py` | 每轮终了异步调 judge LLM 打分，写 METRICS | 质量变可度量/可对比/可告警指标 |
| A/B 测试 | `utils/ab_test.py` | 按 device_id 稳定分桶 + 采样覆盖包装 | 可控实验；桶分配计数进指标 |

### 1.1 集成落点（均已在 `connection.py` 落地，标注 `⑭真进化`）

- **A/B 注入**：`connection.py:_initialize_components`（`self.llm = modules["llm"]` 处）→ 若 `ab_test.enabled` 且 `device_id` 就绪，用 `ABTest.wrap` 包装 `self.llm`，记录 `self._ab_bucket`。
- **离线兜底**：`connection.py:chat()` 的 `except Exception`（原 `return None`）→ 改调 `self._send_offline_fallback(depth)`，下发兜底话术 + `SentenceType.LAST`。
- **judge 钩子**：`chat()` 终点（终轮 depth==0、有完整 `text_buff`）→ `asyncio.run_coroutine_threadsafe(self.dialogue_eval.evaluate_async(...), self.loop)`，后台异步、**绝不阻塞主对话**。

### 1.2 judge 调用范式（复用 intent_llm 三段式）

```
asyncio.wait_for(
    loop.run_in_executor(_JUDGE_EXECUTOR, lambda: self.evaluate(...)),  # 专用 4 线程池
    timeout=self.timeout,                                                # 默认 8s
)
```
- 超时/异常全部吞掉并计 `fuguoai_eval_timeouts_total` / `fuguoai_eval_errors_total`，**主对话零影响**。
- judge 用独立 LLM 配置块（默认 `OpenAILLM`，与线上激活块一致），与对话主流量共享底层熔断器（judge 低频率 + 短超时，正常不污染主熔断）。

### 1.3 指标（进 METRICS，供 `/metrics` 抓取）

- `fuguoai_eval_score{dim, bucket}` histogram —— 各维度 1-5 分
- `fuguoai_eval_total{device_id, bucket}` counter
- `fuguoai_eval_errors_total` / `fuguoai_eval_parse_errors_total` / `fuguoai_eval_timeouts_total`
- `fuguoai_ab_bucket_total{bucket}` counter
- `fuguoai_offline_fallback_total{device_id}` counter

## 2. 配置（config.yaml 根，默认关闭；159 部署时开启）

```yaml
offline_fallback:
  enabled: true
  answer: "我是复果，现在脑子有点转不动，稍后我们再聊～"
dialogue_eval:
  enabled: true
  llm: "OpenAILLM"      # 与 selected_module.LLM 一致的激活块
  timeout: 8.0
  max_text_len: 2000
ab_test:
  enabled: true
  buckets:
    A: {}                # 对照桶：原样
    B:
      overrides:
        temperature: 0.95   # 实验桶：更高采样温度，可测量差异
```

## 3. 真进化验证（离线测试，已入 `tools/regression_gate.py` L3）

- `test_offline_fallback_evolution.py`：兜底文本来源优先级（answer > system_error_response > 默认）。
- `test_ab_test_evolution.py`：稳定分桶（同设备同桶）、覆盖注入（B 桶 temperature 落到 LLM 调用）、disabled 原样返回、单例。
- `test_dialogue_eval_evolution.py`：分数 JSON 解析（含越界 clamp/缺失维度）、disabled 短路、打分成功写 METRICS、judge LLM 异常降级返回 None 且计 errors。

## 4. 部署与回滚

- 部署：逐项 `docker cp`（connection.py + utils 下 3 文件）→ 清 `__pycache__` → 改 config.yaml → restart。
- 不碰 `data/ models/ music/ config.yaml 之外的密钥`；回滚点 `pre-phase5-20260730` 已含 phase5，本次在其上叠加，固化新镜像 `prod-20260730-eval`。

## 5. 后续

- A/B `block` 变体（不同模型块对照）当前仅支持 overrides，可扩展 `wrap` 走 `create_instance` 建独立实例。
- judge 分数可接告警（某桶 overall 均值跌破阈值 → 自动告警/暂停实验桶）。
- 维度②长期记忆+人格、维度③个性化语音克隆为下一阶段创新方向。
