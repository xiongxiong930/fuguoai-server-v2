# 上游解耦（UPSTREAM DECOUPLING）状态页

> 本文件回答一个问题：**本仓相对上游 xiaozhi-esp32-server 的解耦进行到哪一步了**。
> 证据链明细见 `docs/clean-room-methodology.md`（方法论）+ `docs/similarity-audit.md`（逐文件相似度与判据）+ `docs/behavior-gate.md`（行为门验证记录）。

---

## 1. 当前状态（2026-11-01 · 应用源码层已收口 `[硬·实测]`）

| 维度 | 状态 | 证据 |
|---|---|---|
| 逐文件判据 | **122/122 全集映射完成**：C·门证 / C·阈值 / R 契约锁定 / F 结构性地板 / N 复果自有 | `docs/similarity-audit.md` §3/§4/§8 |
| 真派生（X，未处理） | **0**——唯一 ≥0.40 漏网 `get_time.py`（0.417）已于 2026-11-01 clean-room 重写至 **0.327** | similarity-audit §8.1 + `audit/get_time_gate.py` 21 断言 |
| 行为等价 | **60 门 aggregate PASS=1272 FAIL=0**，全部 exit 0 | `audit/_run_all_gates.py`（Python 3.11） |
| 运行时代码残留 | `fuguoai_core/ fuguoai_plugins/ fuguoai_config/ app.py websocket_server.py connection.py` 中 **xiaozhi / xinnan / MIT License 字面量为零** | grep 终检 2026-11-01 |
| 相似度分布 | 122 件 LOGIC 均值 0.455；<0.40 达标 63 件；≥0.40 的 59 件全部有 R/F/C·门证判据 | `tools/scan_derivation_v2.py` 实跑 |
| git 历史 | v2 仓为干净历史起点（首提交即摆脱旧仓完整派生历史）；旧仓仅本地备份、不再对外 | 本仓 `git log` |

## 2. 复现方式

```bash
# 行为门（需 Python 3.11 + requirements；3.13 无 audioop 会导入即崩）
python3.11 audit/_run_all_gates.py          # 期望: ALL 60 GATES PASS (FAIL=0)

# 相似度扫描（WT=本仓、UP=自带快照）
python3 tools/scan_derivation_v2.py         # 期望: 122 个同名文件、exit 0
```

上游对照快照 `_reference_snapshot/main/xiaozhi-server`（xiaozhi-esp32-server @ bd981aa）**不入 git**（体积大且避免把上游源码再度纳入本仓分发），复现时自行获取：

```bash
# 上游为 monorepo，服务端在 main/xiaozhi-server 子目录
git clone https://github.com/xinnan-tech/xiaozhi-esp32-server.git _reference_snapshot/upstream
cd _reference_snapshot/upstream && git checkout bd981aa
mkdir -p ../main
ln -s ../upstream/main/xiaozhi-server ../main/xiaozhi-server
```

## 3. 剩余事项（非应用源码层）

| 事项 | 层面 | 状态 |
|---|---|---|
| 底座镜像 lineage（`server-base` 为 xiaozhi OSS 换皮底座） | Docker 镜像层 | 已在 `THIRD_PARTY_LICENSES.md` 如实披露；彻底重建基础镜像属部署域工程，待排期 |
| 旧仓（fuguoai-server @ phase5）git 历史含派生历史 | 旧仓 | 仅本地备份、不对外发布；如需彻底清除须 `git filter-repo`（破坏性，需显式授权） |
| MIT 归属声明 | 许可文件 | **保留**（合规要求）：`LICENSE` 第 2 段 + `THIRD_PARTY_LICENSES.md` §1；性质为"历史渊源归属"，不覆盖现行自有代码 |
| GitHub 公开发布 | 发布 | 待建仓；发布前自检清单见 `docs/clean-room-methodology.md` §发布前自检 |

## 4. 判据速查

- **C·门证**：clean-room 重写完成 + 行为门交叉互认（LOGIC 不限，高值源于结构地板/契约，由门证背书）。
- **C·阈值**：`LOGIC < 0.40` 即视为重写达标。
- **R 契约锁定**：公开名 / 第三方 URL+字段 / DTO 字段被调用方逐字锁定，`LOGIC<0.40` 数学不可达。
- **F 结构性地板**：逻辑为 fuguoai 自有、由行为门认证等价的 <40 行极小委派/骨架。
- **N 复果自有**：上游无同路径文件（scan 按基名匹配的假阳性，逐一同路径核验排除）。

> 铁律：相似度数字只能排序、不能定性；`LOGIC=1.000` 可能是 0 行空文件 / 导出壳 / ABC 契约地板，≠ 逐字抄。
