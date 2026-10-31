# 创新维度②：长期记忆增强 + 设备人格（LONG_TERM_MEMORY_PLAN）

状态: 实施中 (2026-07-30)
分支: phase5
遵循: PHASE5_PLAN 总原则7（真进化条款）——每模块 ≥1 项可度量真实改进 + 离线测试钉住

## 1. 现状与缺口（07-30 代码探查结论）

现有 `mem_local_short`（生产 `data/.config.yaml` 已启用）具备：
- 按 role_id(=device_id) 分桶的会话摘要（`.memory.yaml`）
- V4 结构化索引（identity/preference/event/topic/fact 五类）+ 词法 RAG（`.memory_index.yaml`）
- `<memory>` 块注入动态 system（connection.py:1501 query_memory → dialogue.py）

**真实缺陷（本轮修复目标）**：

### 缺陷 A：重建即遗忘（压缩损耗累积）
`mem_local_short.py save_memory`: `st["idx"] = entries` —— 每轮索引被
**最新一轮 LLM 总��**整体替换。LLM 总结受上下文与输出长度限制必然压缩，
某轮总结丢掉的旧事实（如两周前说过的生日、忌口）从索引**永久消失**。
会话轮数越多，事实保留率单调下降 → "长期记忆"名不副实。

### 缺陷 B：人格空白
全仓无 persona 概念。所有设备同一套 prompt 人设，无法做到
"爷爷的复果稳重、孩子的复果活泼"。

## 2. 设计

### 2.1 E1 持久事实合并（修缺陷 A）— mem_local_short 增量进化
- 新增 `_merge_index(old, new)`：按条目 id **upsert 合并**，旧条目保留：
  - 同 id：新值覆盖（保留更新语义），`ts` 保留首次时间，`weight` +0.1（封顶 2.0，重复提及=更重要）
  - 不同 id：并集
  - 超上限 `_MAX_IDX_ENTRIES=200`：identity/preference 常驻豁免，其余按 (weight, ts) 驱逐最旧最轻
- `_score_entries` 引入 `weight` 因子：反复提及的事实检索排名更高
- **可度量改进**：多轮总结中被压缩丢弃的事实，合并后保留率 100%（旧行为 0%）；离线测试钉死

### 2.2 E2 设备人格（补缺陷 B）— 新模块 `fuguoai_core/utils/persona.py`
- `PersonaStore`：三层合并 `config.persona.default` ← `config.persona.devices[device_id]` ← `data/.persona.yaml[device_id]`（文件层支持运行时改，mtime 缓存重载，原子写）
- 画像字段：`name`(自称)/`style`(语气)/`traits`(性格)/`catchphrase`(口头禅)/`taboo`(禁忌)
- `render(device_id)` → `<persona>` 中文块；`set_persona()` 持久化（供后续 HTTP API/小程序管理）
- 注入点：connection.py chat() 记忆查询处，`memory_str = persona块 + memory_str`
  （与记忆同通道进动态 system，零改 dialogue.py，memo 键控自动感知变化）
- 指标：`fuguoai_persona_injections_total{device_id}`

### 2.3 三段式纪律
- 人格渲染纯内存操作（文件读带 mtime 缓存），全异常吞掉，绝不阻塞主对话
- 合并逻辑在既有 save_memory 守护线程内，不新增阻塞点

## 3. 变更清单
| 文件 | 变更 |
|---|---|
| `fuguoai_core/providers/memory/mem_local_short/mem_local_short.py` | +`_merge_index` +weight 计分 |
| `fuguoai_core/utils/persona.py` | 新建 |
| `fuguoai_core/connection.py` | persona 初始化 + chat() 注入 |
| `test_memory_merge_evolution.py` / `test_persona_evolution.py` | 新建，入门禁 L3 |
| `tools/regression_gate.py` | L3 列表 +2 |

## 4. 配置（159 容器 config.yaml 追加）
```yaml
persona:
  enabled: true
  default:
    name: 复果
    style: 温暖、简洁、自然口语
  devices: {}
```

## 5. 验证与部署
- 门禁 L1/L2/L3 全绿 → docker cp+清缓存+config 追加+restart → 容器内 import/config 贯通 → 固化镜像（回滚链保留 prod-20260730-eval）
- 真机验收：跨会话提及旧事实命中；per-device persona 生效（改 .persona.yaml 即时生效）

## 6. 红线
- 不改 `.memory.yaml`/`.memory_index.yaml` 文件格式（新旧索引条目结构一致，仅合并策略变化，可回滚）
- 不动 WS 协议 / 既有 HTTP API（persona 管理 API 为**新增**路径，不触碰任何既有路由）

## 7. 人格管理 API（让小程序按设备设人格）

新增 `fuguoai_core/api/persona_routes.py`，由 `registry.apply_routes` 自动发现挂载，
零改 http_server 既有路由。复用 `core.guards.api_guard`（小程序 JWT）+ `DeviceBindingStore`
做设备归属校验（非本人/不存在统一 404，不泄露设备存在性）。

存储 key 一律 `normalize_device_id(device_id)` —— 与 connection.py:291 的 `self.device_id`
规范形态**完全一致**，保证 API 设的人格下一轮对话设备侧 `render` 即可读到。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/persona/{device_id}` | 读合并画像（含默认/配置层）+ 文件层自定义部分 `custom` |
| PUT | `/api/v1/persona/{device_id}` | 部分更新文件层；仅传要改字段；显式 `null` 清除；`traits`/`taboo` 支持逗号串 |
| DELETE | `/api/v1/persona/{device_id}` | 清除文件层，回到默认/配置层 |
| GET | `/api/v1/persona/{device_id}/schema` | 字段元数据（前端渲染表单用，静态） |

数据层直接复用维度②E2 已落地的 `PersonaStore`：
- `get_file_layer(device_id)` 只读文件层（不合并），供部分更新先读旧值；
- `update_persona(device_id, patch)` 部分合并 + 字段级清除 + 类型校验（字符串/列表）；
- 写入 `data/.persona.yaml`（原子写），下一轮对话 mtime 失效即生效，**无需重启**。

字段：`name`(自称) / `style`(语气) / `traits`(性格,list) / `catchphrase`(口头禅) / `taboo`(禁忌,list)。

可测性：`test_persona_api_evolution.py`（9 断言）入门禁 L3，钉死 `update_persona` 的
部分合并 / null 清除 / 未知字段忽略 / 类型校验 / 逗号串拆分语义。

部署：docker cp `persona.py`+`persona_routes.py`+测试+门禁 → 清缓存 → restart →
容器内 OPTIONS 探活路由已注册 → 固化 `prod-20260730-persona`（回滚链保留）。
