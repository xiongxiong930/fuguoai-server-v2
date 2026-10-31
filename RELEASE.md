# 复果AI 后端 · Release 与依赖审计规范

> 本规范定义复果**自有**的版本体系与运维自转机制，保障独立演进过程中的
> 依赖安全、发版秩序与可回溯性。

---

## 1. 版本体系

- **复果自有语义化版本**：`MAJOR.MINOR.PATCH`，不复用任何第三方版本标识。
- 语义：
  - `MAJOR`：不兼容变更（WS 协议断更、DB schema 破坏性迁移、设备端契约 breaking）
  - `MINOR`：向后兼容的新功能/新模块（如新增对讲模式、设备面板）
  - `PATCH`：修复/优化/依赖安全升级，向后兼容
- **当前稳定基线**：`1.2.0`（tag `v1.2.0`；`SERVER_VERSION` 已进位 `1.2.0`）。
- **运行时版本源**：`fuguoai_config/logger.py` 的 `SERVER_VERSION`（设备/运维展示、日志头）。发版时同步更新。
- **tag 规则**：`v<MAJOR.MINOR.PATCH>`（替代旧日期 tag 如 `v20260725`）。tag 附注发版日期与一句话摘要。

---

## 2. 依赖审计（核心自转机制）

- **工具**：`tools/dep_audit.py`（纯标准库，查 PyPI latest 版本；可选地调用 `pip-audit` 做 CVE 扫描）
- **节奏**：
  - 每月 1 日：月度自动化提醒执行一次（见第 5 节）
  - 每次发版前：**必跑**
- **判定与处置**：
  | 状态 | 含义 | 处置 |
  | --- | --- | --- |
  | `OUTDATED` | `==` 固定且落后 latest | 评估升级，安全相关优先 |
  | `UNPINNED` | 无上限约束（如 `powermem>=0.3.1`） | 评估锁定到具体版本，避免隐式漂移 |
  | `PINNED-OLD` | `torch`/`numpy`/`websockets` 等有意锁定且落后 | **确认兼容才升**，勿盲目（牵一发动全身） |
  | `LATEST` | 已是最新 | 无需动作 |
  | `UNKNOWN` | 网络/PyPI 不可达 | 重试，不阻塞 |
- **CVE（High/Critical）**：**24 小时内**评估并出 PATCH 版本。
- **产出**：`tools/dep_audit_report.md`，重大升级记入 `CHANGELOG.md`。

---

## 3. Release Checklist（发版前按顺序）

1. [ ] `git pull` 最新 `main`，全量 `python -m py_compile` 无错
2. [ ] 跑依赖审计 `python tools/dep_audit.py`，处置过期/CVE（结论进 CHANGELOG）
3. [ ] 跑回归 `bash run_tests.sh`，全绿
4. [ ] 更新 `CHANGELOG.md`：新增 `## [X.Y.Z] - YYYY-MM-DD` 段，列变更/依赖变动
5. [ ] 更新 `fuguoai_config/logger.py` 的 `SERVER_VERSION = "X.Y.Z"`
6. [ ] 本地 `py_compile` + 关键模块 import 自检
7. [ ] 部署（遵循项目铁律：`py_compile → scp → docker cp → 清 __pycache__ → restart → 活文件 grep 核实`）
8. [ ] `git commit` + `git tag vX.Y.Z` + 推送
9. [ ] CHANGELOG 顶部标注本版本为复果自有 release（来源 `fuguoai-server` 仓库）

---

## 4. 安全与责任

- 依赖安全更新、CVE 修复、协议演进由复果自建流程保障（即本规范 + `dep_audit.py` + 月度提醒）。
- 如需借鉴外部某次修复，手动 cherry-pick 并注明来源 PR/commit，不自研管线引入未经验证的第三方合并。
- 固件端（`fuguoai-ble-firmware`）维持现状，仅做文本层去引用；唤醒词模型为 ESP32 自带 wakenet，不替换。后端只要 WS 协议格式保持兼容即与现有固件互通。

---

## 5. 自动化

- 已建**月度 recurring 自动化**（每月 1 日触发）：提醒执行 `python tools/dep_audit.py` 并复查 `dep_audit_report.md`。
- 项目无 CI 基础设施，审计为**本地/容器手动触发 + 月度提醒**模式，不强制云端执行。
- 如需升级为自动出 PR/自动扫描，需后续引入 CI（不在本规范范围）。

---

## 6. 参考副本管理

- 早期所基于的参考实现已隔离为 `_reference_snapshot/`，仅作历史架构参考，不读、不合并、不发布。
- 复果演进以 `fuguoai-server` 仓库（bare origin `<AUTH_HOST>:/opt/git/fuguoai-server.git`）为准。
- 历史快照与当前代码的具体差异属实现沿革，不影响当前版本责任归属。
