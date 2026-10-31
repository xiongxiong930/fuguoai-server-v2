# 维度⑤ 家庭 RAG 知识库（创新维度⑤）

> 让设备"记住家里的事"——家庭成员、习惯、物品、日程、偏好等，并在相关对话时语义召回注入。
> 纯后端可落地，不依赖固件改动。与维度②长期记忆（对话摘要索引）和现有 MemoryRAG（关键词 RAG）**互补不冲突**。

## 1. 动机与差异化

现有记忆体系：
- `mem_local_short` V4 结构化索引 = **对话摘要**的检索（"我们之前聊过什么"）
- `MemoryRAG` = 关键词/结构化 **RAG**（基于摘要索引的词法匹配）

二者都围绕"对话内容"，**没有"家庭客观事实"这一层**。用户说"我家猫叫咪咪""爸爸对花粉过敏""娃上幼儿园了"，这些事实散落在对话里，下次问"猫粮在哪"无法召回。

维度⑤新增**家庭事实知识库**：结构化存储家庭客观事实，语义检索注入，使设备具备"家庭管家"质感。这是强智能差异化维度。

## 2. 与现有 RAG 的关系

| 层 | 来源 | 检索方式 | 用途 |
|----|------|----------|------|
| 长期记忆(mem_local_short) | 对话摘要 | 关键词+结构化索引 | "之前聊过 X" |
| MemoryRAG | 同上 | 关键词 RAG | 摘要检索增强 |
| **家庭 RAG(本维度)** | **LLM 抽取的家庭事实** | **向量语义(可插拔 local/openai)** | **"家里有猫叫咪咪"** |

三者独立存储、独立注入，互不干扰。家庭 RAG 走独立的 `<family_knowledge>` 注入块。

## 3. 架构

### 3.1 Embedding Provider（可插拔）
新增 `fuguoai_core/providers/embedding/`：
- `base.py` — `EmbeddingProvider` 抽象：`embed(texts: list[str]) -> list[list[float]]`，进程级缓存
- `local_hash.py` — `LocalHashEmbedding`：**numpy TF-IDF 词频余弦**，零外部依赖。用于①离线测试 ②远程 embeddings 不可用时的降级（保证部署不阻塞）
- `openai_embedding.py` — `OpenAIEmbedding`：复用 LLM 的 openai-compatible 客户端范式（`base_url`/`url` + `api_key` + `model_name`），调 `client.embeddings.create(...)`。**真语义**来源

配置 `Embedding` 块（与 `LLM` 同范式）：
```yaml
Embedding:
  type: local        # local | openai
  # type=openai 时：
  # base_url: https://api.openai.com/v1
  # model_name: text-embedding-3-small
  # api_key: sk-xxx
```

### 3.2 家庭知识库 `fuguoai_core/utils/family_rag.py`
- `class FamilyRAG`：`enabled`、`embedding`(provider)、`top_k`
- **录入** `add_fact(device_id, content, category, tags)`：embed(content) → 追加 `data/family_rag/{device_id}.json`（原子写 temp+rename）
- **抽取** `extract_and_store(device_id, query, reply, config)`：**三段式后台 LLM**（复用 dialogue_eval 范式：专用线程池 + 超时 + 全异常吞），从"用户问+设备答"抽取家庭事实 JSON 数组 `[{content, category, tags}]`，逐条 add_fact
- **检索** `retrieve(device_id, query)`：embed(query) → top-k 余弦 → 返回事实文本列表
- **渲染** `render(device_id, query)`：生成 `<family_knowledge>` 块
- `get_family_rag(config)` 进程单例
- 指标：`fuguoai_family_rag_facts{device_id}`、`fuguoai_family_rag_retrieves_total`、`fuguoai_family_rag_extract_errors_total`

### 3.3 存储格式
`data/family_rag/{normalized_device_id}.json`：
```json
{
  "facts": [
    {"id": "sha1", "content": "我家猫叫咪咪，是橘猫", "category": "pet", "tags": ["猫","咪咪","宠物"], "vec": [0.1, ...], "ts": 1234567890, "weight": 1.0}
  ]
}
```
- 存储 key 用 `normalize_device_id`，与 `connection.py:291 self.device_id` 规范形态一致
- 向量内联存储（维度小，local 版 ~256 维 numpy，openai 版 1536 维），单设备家庭事实量级小（数十~数百条），无 faiss 必要

## 4. 真进化点（≥1 可度量改进 + 离线测试）

**E1 家庭事实语义检索**
- 旧：无家庭事实层，相关询问无法召回（如"猫粮在哪"→无响应或泛答）
- 新：对话抽取家庭事实 → 向量检索 → 相关对话注入，召回家庭客观事实
- 度量：离线测试构造 query/事实集，验证 top-k 命中率（LocalHashEmbedding 同义/近义词面相关召回；远程 openai 版真语义）；指标 `fuguoai_family_rag_retrieves_total`/`facts`

**E2 对话自动抽取家庭事实（零手动录入）**
- 复用 dialogue_eval 三段式：每轮首层对话(depth==0)后后台 LLM 抽取，用户无感
- 全异常吞：抽取失败/超时不影响主对话，仅计 `extract_errors_total`
- 度量：离线测试 mock LLM 返回结构化 JSON → 验证 add_fact 入库 + 去重

## 5. 配置（追加到 config.yaml）
```yaml
FamilyRAG:
  enabled: true
  top_k: 5
  embedding:
    type: local        # 默认 local，零依赖立即可用；配 Embedding 块+type=openai 升级真语义
  extract:
    enabled: true
    trigger_depth: 0   # 仅首轮对话后抽取
```

## 6. 集成落点（connection.py）
- import `get_family_rag`（⑭⑮ 同位置）
- `_initialize_components`：`self.family_rag = get_family_rag(self.config)`
- `chat()` 记忆注入通道（persona 之后）：`if self.family_rag.enabled and self.device_id: family_block = self.family_rag.render(self.device_id, query)` 注入 system
- `chat()` 终点（存储对话后）：`if depth==0 and self.family_rag.enabled and self.family_rag.extract_enabled: asyncio.run_coroutine_threadsafe(self.family_rag.extract_and_store(self.device_id, query, text_buff, self.config), self.loop)`
- 全异常吞，绝不阻塞主对话

## 7. 离线测试（真进化条款，入门禁 L3）
`test_family_rag_evolution.py`：
- LocalHashEmbedding 余弦：构造相关 query/事实，验证 top-k 命中 + 不相关不进 top-k
- add_fact + retrieve 往返：录入后检索返回该事实
- 录入去重：同 content 不重复（id 稳定 = sha1(content)）
- render top_k 截断：超过 top_k 只取前 k
- OpenAIEmbedding 异常降级：mock client 抛异常 → 降级 LocalHashEmbedding 或返回空，不抛

## 8. 部署与回滚
- 部署铁律：docker cp + 清 `__pycache__` + config 追加 + restart
- 容器内验证：import OK；LocalHashEmbedding 检索往返；extract 入库（可用探针）
- 固化新镜像 `prod-20260730-rag`，保留回滚链
- 本地 git commit

## 9. 后续（非本轮）
- 家庭知识录入 API（类似 persona_routes：PUT/DELETE /api/v1/family-knowledge/{device_id}），让小程序主动录入
- 远程语义升级：配置 `Embedding.type=openai` + 端点，一键从 local TF-IDF 升级真语义
- 事实置信度/冲突消解（同主题新旧事实合并）

## 10. 红线
- 不改现有 MemoryRAG / mem_local_short 文件格式与行为（家庭 RAG 独立存储）
- HTTP API 只扩不删改（录入 API 留待下轮）
- 不碰 data/ 真实密钥；embedding 向量内联存储无新增密钥
- 抽取/检索全异常吞，绝不阻塞主对话
