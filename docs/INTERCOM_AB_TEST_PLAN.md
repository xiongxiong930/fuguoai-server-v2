# 对讲联调 A/B 验收计划（60ms 探针 → 20ms 回退对照）

> 制定：2026-07-28 晚（为次日真机联调准备）。容器安全：本计划**不部署任何代码**，只固化步骤/预期/命令，待次日板子插上按表执行。
> 当前 159 容器：`fuguoai-server:intercom-60ms-20260728`（60ms 版，17:45 已落盘定罪）。
> 回滚链（均在）：recovered-20260725 → pre-p4b-20260728 → p4b-20260728 → p4c-20260728 → tts-diag-20260728 → intercom-60ms-20260728。

---

## 0. 背景与已坐实结论（不必重测）

17:45 那通（60ms 版）离线落盘已一锤定音：

| 指标 | 60ms 版实测 | 结论 |
|------|------------|------|
| 设备上行帧率 | **14.6 fps**（应 50，约 0.29×） | 🔴 60ms 大帧把设备端音频循环拖慢 3 倍 |
| 网关逐 10s 帧计数 | 146/145/147/145/144/145（极稳定涓流） | 设备侧收发同 WS 链路被背压卡住 |
| 挂断后尾巴 | 上行止于 17:45:10，设备发到 17:46:23 → **~73s 尾巴** | 上行被 60ms 大帧节流后堆积延迟 |
| 双端杂音 | 设备喇叭 + 手机**都仍有杂音** | 帧时长错配非唯一根因；B1/B2 另查 |
| 实时率 0.34 | = 统计 bug（按 20ms 口径低报 3 倍），×3 = 1.02 | 网关零堆积，已修（02b2649） |

**⇒ 60ms 假设已被推翻为「有害」**：它制造设备上行 3 倍减速 + 73s 尾巴，且未消除杂音。机制假设③（60ms 大帧 → WS send/recv 锁竞争 → 上行涓流）待固件探针钉死，但「回退 20ms」是确定的修复方向。

---

## 1. 次日两通对照流程（板子插上后执行）

> 若嫌两通麻烦，可**直接跳到第 2 步部署 20ms**（代价：机制③停在假设，但 20ms 本身是正确方向，体验必优于 60ms）。

### Pass 1 — 60ms 探针通（确认机制③，可选但推荐）
- 容器仍是 60ms 版（未动）。
- 159 flag 已重置、旧 bin 已清（在 `data/tmp/`）：
  ```bash
  ssh -i ~/.ssh/id_ed25519 ubuntu@<PROD_HOST> \
    "sudo -n docker exec fuguoai-server sh -c 'cd /opt/fuguoai-server/data/tmp && rm -f .intercom_timing .intercom_rawdump intercom_uplink_raw.bin intercom_downlink_raw.bin intercom_downlink.pcm 2>/dev/null; touch .intercom_timing .intercom_rawdump; ls -a'"
  ```
- 固件 owner 板 A 已烧 3 探针（设备上行 fps / downlink arrival+queue / SendAudio 阻塞 µs）。
- 触发一次对讲（听到杂音段），结束后拉 bin 离线解码：
  ```bash
  scp -i ~/.ssh/id_ed25519 ubuntu@<PROD_HOST>:/opt/fuguoai-server/data/tmp/intercom_uplink_raw.bin /tmp/
  scp -i ~/.ssh/id_ed25519 ubuntu@<PROD_HOST>:/opt/fuguoai-server/data/tmp/intercom_downlink_raw.bin /tmp/
  ```
- **预期**：设备 fps ≈ 14.6 + arrival ≈ 60ms/帧 + avg_block 偏大 → 钉死机制③。

### Pass 2 — 部署 f2096b8 回退 20ms
- `f2096b8`（Revert "60ms"）= 恢复逐 640B / 20ms 编码，已含 `02b2649` timing 修正，在 `main` 与 `feature/tier1` 工作树中均已是 20ms 版（`grep -c buf60 intercom_registry.py` = 0）。
- 部署（脚本用**本地工作树文件**，第二个参数是镜像 tag）：
  ```bash
  # 在当前已切到 main 或 feature/tier1 的工作树下执行
  bash tools/deploy_file.sh fuguoai_core/utils/intercom_registry.py intercom-20ms-20260729
  ```
- 铁律链自动走：本地 py_compile → scp → docker cp → 清 `__pycache__` → restart → **活文件 md5 核实** → docker commit（`fuguoai-server:intercom-20ms-20260729`）。
- 部署后必查：容器 `Up`、8000(WS)/8002(HTTP) LISTEN、启动无 ERROR/Traceback；`grep buf60` 在容器内应为 0（确认 60ms 代码已不在）。

### Pass 3 — 20ms 对照通（验收主通）
- 重新重置 flag + 清 bin，再触发一次对讲（与 Pass 1 同样时长/场景）：
  ```bash
  ssh -i ~/.ssh/id_ed25519 ubuntu@<PROD_HOST> \
    "sudo -n docker exec fuguoai-server sh -c 'cd /opt/fuguoai-server/data/tmp && rm -f .intercom_timing .intercom_rawdump intercom_uplink_raw.bin intercom_downlink_raw.bin intercom_downlink.pcm 2>/dev/null; touch .intercom_timing .intercom_rawdump'"
  ```
- 拉 bin 离线解码比对（同 Pass 1 命令）。

---

## 2. 预期指标对照表（验收判定依据）

| 指标 | 60ms 版(预期基线) | 20ms 版(预期改善) | 判定 |
|------|------------------|------------------|------|
| 设备上行帧率 | 14.6 fps | **≈ 50 fps** | ✅ 回 50 即机制③坐实且已修 |
| 挂断后尾巴 | ~73 s | **≈ 0 s**（挂断即停） | ✅ 尾巴消失 |
| avg SendAudio 阻塞 µs | 偏大（量级高） | **掉一个量级** | ✅ |
| 下行到达间隔 | 60 ms/帧 | **20 ms/帧** | ✅ |
| 实时率 | 0.34（bug）→ 1.02 | ≈ 1.0 | ✅ 零堆积 |
| **B1 设备喇叭出声** | 不出/杂音 | **干净人声**（需真听） | ⚠️ 验证固件 owner 的 EnableOutput 修复 |
| **B2 上行 20s 硬截止** | 前 20s 强噪后硬崩 | 仍需固件探针钉根因 | ⚠️ 非网关（网关已洗清） |
| **杂音归属** | 双端杂音 | 若仍在 → 固件采集/AEC（网关已排除） | ⚠️ 待真听 + 字节诊断 |

> **网关侧已彻底洗清**（16:57 / 17:45 两通落盘铁证）：uplink 2998 帧全合法发出、downlink 发手机 640B 裸 PCM 无头、decode err=0。20ms 对照通若仍有杂音/硬崩，100% 在固件侧，勿再回头查 relay。

---

## 3. 定罪矩阵（Pass 3 后逐项勾选）

- [ ] **设备上行帧率回 50fps**（网关日志逐 10s 帧计数 ≈ 500/10s）→ 机制③钉死 + 修复生效
- [ ] **挂断尾巴消失**（uplink 末帧 ≈ 挂断时刻，无 73s 拖延）
- [ ] **B1 设备喇叭出声**（真听：干净人声，非杂音/非静音）→ 固件 EnableOutput 修复验证
- [ ] **B2 探针数据**（固件 owner 提供：in_intercom_ 翻转? / SendAudio 丢帧? / WS 重连未恢复?）→ 钉 20s 硬截止根因
- [ ] **杂音是否仍在**：在 → 固件采集增益/AEC（网关无关）；不在 → 60ms 次生现象已随回退消失
- [ ] **对讲整链路体验**：双端出声、延迟可接受（<1s）、无 60s 缓冲

---

## 4. 验收判定（GO / NO-GO）

> ⚠️ **发布解耦声明（2026-07-29 修订）**：本 GO/NO-GO **仅代表固件声学 track 的验收结论**，不再作为「功能分支合 main / 打 v1.0.0」的发布闸。
> 经 git 实测：4 个功能分支（tier1 / networked-reminder / device-panel / proactive）**未改动任何对讲代码**（intercom_* 文件与 main 逐字节相同），且 60ms→20ms 回退（commit `f2096b8`）已含于 main 与全部功能分支。
> ⇒ **功能发布与对讲全双工解耦**：4 功能独立打 `v1.0.0` + 部署 159，**不等待本 GO**；本 GO 仅用于固件 B1/B2 验收与噪声归属闭环，并顺带把 159 当前有害的 60ms 版修回 20ms。

**GO（对讲 M1 声学验收闭环）** 当：
1. 设备上行帧率 ≈ 50fps 且挂断尾巴消失；**且**
2. B1 设备喇叭能出干净人声（真听确认）；**且**
3. 用户主观体验「延迟正常、双端出声」。

→ 固件 track 收尾：①固件 B1/B2 验收闭环（修复权在固件团队，红线）②噪声归属确认（网关侧已自证无责）③功能发布已先行独立推进（见上方发布解耦声明），本 GO 不再触发合 main / 打 v1.0.0。

**NO-GO / 待固件** 当：
- B1 仍不出声 / B2 仍 20s 硬崩 / 杂音仍在 → 这些**修复权在固件团队（红线）**，网关侧已自证无责，整包转固件 owner，附本计划第 2-3 节铁证。

---

## 5. 风险与回滚

- 部署脚本已含 md5 核实 + 自动 commit，单文件部署可逆：`sudo -n docker commit fuguoai-server intercom-60ms-20260728` 已在；若 20ms 异常，回滚 `sudo -n docker tag intercom-60ms-20260728 fuguoai-server:rollback && sudo -n docker restart fuguoai-server`（或从既有镜像 `docker commit` 重建）。
- 只动 `intercom_registry.py` 单文件，不影响 tier1 其余模块、不影响 8000 WS 协议、不影响 HTTP API。
- 容器内生产配置（`data/.config.yaml`）、模型、运行时产物全保留；部署铁律全程不删目录。
- 159 主机：`ubuntu@<PROD_HOST>`，key `~/.ssh/id_ed25519`，docker 需 `sudo -n`。

---

## 6. 执行清单（次日照勾）

- [ ] 板子插上、固件 owner 确认探针版烧录
- [ ] Pass 1：60ms 探针通 → 拉 bin → 确认 fps≈14.6 / 机制③
- [ ] Pass 2：`bash tools/deploy_file.sh ... intercom-20ms-20260729` → md5 核实 OK → 8000/8002 通
- [ ] Pass 3：20ms 对照通 → 拉 bin → 逐项勾 §3 定罪矩阵
- [ ] 判定 GO/NO-GO → 仅通知固件 track 结论（功能发版已解耦先行，详见 §4 发布解耦声明）
