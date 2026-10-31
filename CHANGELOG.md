# 复果 AI 后端 CHANGELOG（自研 / self-evolved）

> 本项目为**独立演进**代码库，采用复果自有版本体系与发版节奏。
> 完整历史见 `git log`（共 120 commits，最新 `c349ac7`）。本文件为主要里程碑摘要。

## v1.2.0 — 日程同步 id 命名空间修订 + once 清理闭环 + admin/cards（2026-10-18）

> 进位 `1.1.0` → `1.2.0`。归并 159 已部署、经用户授权进版本的改动（守"先别固"，实测验证后进位）。
> 专题设计文档：`docs/SCHEDULE_SYNC_ID_NAMESPACE_20261018.md`。
>
> **范围说明**：本版本仅含后端自有域改动（日程镜像 B 方案 + admin/cards 4 端点）。并行线
> （cosyvoice / router / helloHandle / intercom_call_store / INTERCOM_ADVICE）未纳入，保持独立演进。

### 根因：cJSON `valueint` 溢出钳位（8818 实测三次覆盖）
- 固件 `JsonToTask` 用 cJSON 读 `it->valueint`（int），>INT_MAX 的数被钳成 **2147483647**
- 旧约定云端 id 从 `0x80000000`（2147483648）起递增 ⇒ 设备侧所有云端 id 全变同一个值 ⇒ 设备表**只有一个云端任务槽位**，后到者 LWW 覆盖先到者（早餐→喝水10:30→喝水10:50 三次覆盖，屏上永远只剩 1 条云端日程；用户反馈"以前 4 条现在 2 条"即此）

### 新约定：云端 id 从 INT_MAX 递减分配
- `schedule_mirror_store`：`CLOUD_ID_MAX=0x7FFFFFFF` 起递减（下限 `CLOUD_ID_FLOOR=1e9`，耗尽抛 `ValueError`）；≤INT_MAX 不被钳位，多云端任务可共存
- 与本地 id 不碰撞：固件 `AddTask` 对无高位 bit 的 id 推进 `next_id_`，首个 INT_MAX 落位后本地 id 往上、云端 id 往下**永不相交**
- 命名空间判定改认显式 `source` 字段（"cloud"/1）；`CLOUD_ID_HIGH_BIT` 降级为仅识别迁移前 legacy 存量

### once 清理闭环（用户实测"过时间了也没清除"）
- create 工具把 rid 写入镜像条目；`fire_one` 播报成功后 once 类型按 rid 删镜像 + 经 `schedule_push` deletes 下行（daily/weekly 不清理；best-effort 异常不影响主流程）
- cancel 路径 rid 精确删除优先，内容匹配仅作 legacy 无 rid 存量兜底（同内容多条提醒不误删）

### pull 回灌带 stale deletes（新机制）
- `schedule_device_tables.json` 原样存设备 M1 全表；pull 时「设备表 cloud ids − mirror cloud ids」= 应删 id，补删 fired/cancel/迁移残留（旧 pull 永远 `deletes=[]`）

### 存量迁移（tools/migrate_schedule_mirror_ids_20261018.py）
- 备份 → cloud 条目按 `modified_utc` 降序重映射到 `INT_MAX-100` 起递减区间 + bump `modified_utc` + 删已过期 once + 补 `source` 字段；干跑/apply 两段式

### 测试
- `test_schedule_mirror_store.py`（25 例）：递减分配/下限耗尽/source 判定/id 区间兜底/by_rid 删除/设备表/stale deletes/legacy 兜底
- `test_schedule_downlink.py`（21 例）：pull stale deletes 差集 / M1 设备表缓存 / **source 透传防回归**（屏显剥离尝试的回退护栏）
- `test_networked_reminder_mirror.py`（19 例）：rid 闭环（create 写 rid / cancel rid 精确删 / 同内容不误删 / legacy 兜底）
- `test_networked_reminder_fired_cleanup.py`（新增 5 例）：once 触发清镜像 + deletes 下行 / daily 不清理 / 无 rid 不 push / 异常不阻断 / 缺失条目 no-op

### 屏显 [云·通]：后端不可修，待固件决策（回退记录）
- 用户两次反馈语音设的提醒屏显 `[云·通]`。曾尝试下行剥离 source 让设备显示 [本·通]，**实测不可行且有害已回退**：① 固件对 schedule_push 到达任务统一标 kCloud（8818 实证无线 source 仍存 source=1）；② `HandleSchedulePush` deletes 只删 kCloud/高位 id 任务（device_control.cc:1626），新 id 无高位 bit，设备存 kLocal 后 fired once 删除被静默跳过 ⇒ 清理闭环失效。
- 结论：屏显由固件 `ScheduleRowPrefix` 单方决定，source 字段被屏显与 deletes 共用、语义冲突，后端无法两全。选项（去掉云/本前缀 / 统一 [本·通] / 维持现状）待用户拍板，涉固件烧录 + 跨团队协同，后端不擅动。详见设计文档 §8。

### admin/cards 管理后台 4 端点（2026-10-18，第 12–13 轮）
- 新增 `fuguoai_core/api/admin_cards_routes.py`，由 `registry.apply_routes` 自动挂载（扫 `core/api/*_routes.py`）。
- 端点：`GET /api/v1/admin/cards`（列表，MOCK 沉底、屏蔽 `card_uid` 仅用 serial 寻址）、`POST /api/v1/admin/cards/release`（admin 强制释放，跳过 `must_wipe` 闸门）、`GET /api/v1/admin/cards/{serial}`（详情）、`POST /api/v1/admin/cards/{serial}/transfer`（转让）。
- `card_registry.py` 配套：`list_all()`（MOCK 沉底、不下发 `card_uid`）、`admin_release(card_uid)`（admin 强制释放）。
- **前端契约**：时间字段统一 `YYYY-MM-DD`（`strftime("%Y-%m-%d")`，北京时间），修复 43 管理后台 `iso.split is not a function` 崩溃与 `NaN-aN-aN` 显示。
- 防克隆：列表/详情响应体不暴露 `card_uid`，仅以 serial 寻址。

## v1.1.0 — 角色契约 / 音色 / 记忆分层 / 文旅 KB（2026-10-18）

> 版本进位 `1.0.1` → `1.1.0`。归并 `v1.0.1`（`c04dc18`）之后 phase5 上的 **39 个 commit**（38 个非 merge）。
> 全部为向后兼容的新功能与修复，无 WS 协议 / DB schema breaking ⇒ 按语义化版本规则进 **MINOR**。
>
> ⚠️ **未含**以下「已部署 159 但尚未 commit」的改动（不计入本版本，待各自 commit 后再进位）：
> `family_rag.py` 相关度闸门（min_score/rel_gap）、`networked_reminder_routes.py` 8818 提醒加固。

### 角色 / NFC 契约域（最大块）
- **角色分层与卡注册表**（P1~P3）：`tier` 字段 + `card_registry`（card_uid 白名单）+ 领卡/碰卡守位；无卡短期记忆 / 持卡长期记忆真正生效；拥有感四件套（我的卡/履历/一键清空/转让/收藏）
- **owned 角色可见性改造**：未持卡从「直接隐藏」改为「可见但锁定」（`locked:True` + 脱敏 system_prompt），`/characters` 目录、activate、messages 三处闸门一致
- **card_uid 归一化**：修同一张物理卡因大小写/分隔符被判成 `unknown_card`
- **NFC 双交互面**：app 面 HTTP `/nfc/tap`、`/nfc/claim` + 设备面 WS `nfc_tap`/`nfc_claim` 文本帧（模型 b，同源复用 `_process_nfc_tap/_process_nfc_claim`），device 面注入角色 system_prompt
- **契约落地**：2.2（activate 附 `transition_line` + tts 参数、返回 `ritual_type` first/wake）、2.4（`/character-binding` 建/换绑）、字段映射线 0（voice_type/background 别名 + 附录 A 三字段）
- **character-binding 权限收紧**：封三洞（owned-tier 闸门 / 会话劫持 / 伪造 serial）
- **locked 角色目录白名单构造**：堵死人格/调校字段泄露
- **角色对话流式链路修复**：多轮历史进 dialogue + 同步 LLM 流下沉，不卡事件循环
- **后端 product 隔离**：活字匣 / 天使心角色按 `product` 分轨，互不串

### TTS 音色与参数域
- 小程序 `/tts` 网关按角色声线透传，修复 5 角色全同声
- 设备面 TTS 按角色音色合成（per-voice 腾讯实例闭环）；`voice_type` 进白名单 + activate/message 出口生效
- `speed` 链路打通：腾讯 Speed 小数归一化 + 端点 per-call 透传
- 腾讯流式 TTS 补 16k→`conn.sample_rate` 重采样，修复设备端 1.5× 快放
- 提醒 TTS 主后端失败自动兜底腾讯，修复 Edge 403 时定时器静音

### 记忆 / 人格域
- 对话历史按 `(uid, character_id)` 分桶 + 落盘持久化（P0）
- 记忆闭环删除端点 + 性格偏移层（persona-offset）
- `preferences` 的 `character_overrides` 改为按 character_id 合并 + 字段白名单
- CharacterProfile 展示字段扩展（backstory/awaken_quote/led_config/awaken_sound）+ to_dict 序列化
- 修复灯效/音效覆盖回读 405 根因

### 认证域
- 微信密钥改由 config（`miniprogram.secret`）优先于 env
- 新增 dev 模式免微信演示登录端点

### 文旅 KB
- 补仙桃（14 条）/ 天门（10 条）两座零覆盖城市，湖北 KB 302 → 326 条

### 测试
- 补 NFC 卡登记、记忆分层、NFC WS 路由、JWT 鉴权、activate/messages/session、卡片生命周期与角色可见性闸门等自进化测试（累计 158 例全绿基线）

## v1.0.1 — PyJWT 安全升级（2026-10-04）
- 依赖安全 PATCH：PyJWT `2.13.0` → `2.15.1`，消除 13 个已知漏洞（1 CRITICAL + 5 HIGH + 7 MODERATE；2026 年新发，OSV.dev 直查）
- 触发：月度依赖审计（RELEASE.md §2 CVE 24h PATCH 机制）；2.13.0 自身漏洞为 2026-10-01 审计新发现，前次 8-31 审计时尚未存在
- 向后兼容：仅 JWT 签发/校验内部修复，无 API 破坏；升级目标 `2.15.1` = PyPI latest 且 0 漏洞
- 同步：`SERVER_VERSION` `1.0.0` → `1.0.1`
- 范围护栏：本次仅动 PyJWT，未混入 `openai 3.x` / `mcp 2.x` 等破坏性主版本升级（留作独立迁移任务）

## v1.0.0 — 首个正式 release（2026-08-11）
- 版本进位：`SERVER_VERSION` `0.9.5` → `1.0.0`，复果自有语义化版本正式启用（tag `v1.0.0`）
- 项目身份独立化：新增 `README.md` 树立复果AI 后端独立项目身份；`UPSTREAM_DECOUPLING.md` 中性化上游残留清理清单
- 相似度度量基线退休上游：锚定本仓 fork 起点（`8f051a8`）自进化度，缺失时自动从 git 重建快照（`tools/similarity_report.py`）
- 上游 provenance 注释软化（`llm/base.py`、`mcp_executor.py`，仅注释改动，行为零变化）
- 仅保留 `THIRD_PARTY_LICENSES.md` 的 Apache-2.0 合规归属声明

## 基础设施与演进策略
- 清理早期基线命名残留（Dockerfile 基础镜像换基 `fuguoai-server-base`、删除外部 config 注释）
- **2026-07-27 确立独立演进与自有 release/依赖审计机制**

## 实时全双工对话（realtime 系列）
- barge-in 门限/残差/AEC 系列优化（P0 ~ P0.6：残差平滑、绝对门限 650→400、播放尾音守卫）
- 全双工识别修复、abort 卡死根治（AEC 残差驱动真打断，忽略设备 legacy abort）
- AEC 回退至服务端时钟对齐（固件同源 ts 导致时基不匹配）
- TTS 兜底音色锁定、主后端失败兜底 TencentTTS、EdgeTTS 连接保活与瞬态重试
- 首包延迟三段埋点（LLM 首 token / TTS 首段合成）

## 对讲 Intercom
- M1：1:1 opus 透传中继（会话映射、忙线、断连 grace、loopback 自环）
- B 系列：呼叫编排层 + pending 补偿 + events 事件（契约 v1.6.0 / v1.8r1）
- B-5：小程序真人语音对讲（模式 B，契约 v1.9）
- relay 对 `role=phone` 做 opus<->PCM 双向转码，并支持 AAC(ADTS) 实时转码（PyAV 持久 codec）
- phone 边界音频编码双模可协商（默认 PCM，兼容旧小程序）

## 设备 / 用户 / 微信生态
- 手机号一键登录 `phone_code`（getPhoneNumber → 绑手机），users 表加 phone 列
- 设备管理 admin API、设备绑定库、预设人格模板（M15）、agent-config 落库
- 设备日程镜像上行、提醒 store（device_reminders 表）、离线 409 守卫
- 记忆文件损坏自愈、设备 ID 大小写归一化、闭嘴/停止说话意图识别

## 质量与可观测
- 对讲 relay 双向字节计数、到达帧/注册态/帧头诊断、重连生命周期日志
- 生产 WARNING 级吞 INFO 诊断日志修复（独立 INFO handler）
- PCM 落盘抓波形工具（flag 触发，联调判定削波/噪声）

## 运维自转机制（2026-07-27 新增）
- `tools/dep_audit.py`：依赖审计脚本（查 PyPI latest + 可选 pip-audit CVE），保障依赖安全
- `RELEASE.md`：复果自有版本体系（语义化 `vX.Y.Z`，从 1.0.0 起）+ release checklist + 月度审计节奏
- 月度 recurring 自动化提醒执行依赖审计（独立演进自转保障）
