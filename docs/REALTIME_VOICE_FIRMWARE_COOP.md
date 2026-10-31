# 实时语音 AEC 参考流 — 后端实测分析报告

> 后端：fuguoai-server（已部署 159，镜像 `aec-clean-20260803`）
> 适用固件：本机固件 realtime 全双工路径（设备 `94a9901a90c0` 融合仓，机主自改自编自烧，无独立固件同事）
> 目的：澄清「AEC 消回声生效率低 / Δt 13.6s / no_candidate 73%」是否固件缺陷。**结论：固件 `audio_playback_ref` 时序正确，无需改动；上述指标是诊断假象。**

---

## 0. 结论速览

| 早先怀疑 | instrument 实证结论 |
|---|---|
| 固件「收到 TTS 即发 ref，早 13-20s」导致 no_candidate | ❌ 错。固件在 `OutputData`（实际播放输出）后逐 chunk 连续发 ref；13.6s Δt 是 **TTS 合成间隙**，非早发 |
| WS 关后须「彻底拆音频管线」否则重连崩 | ❌ 错且危险。管线常驻（Start 一次永不销毁），v1.8.5.5 已修重连崩（指数退避 + `replacing_` 守卫）；拆=回归 |
| 自激（回声误打断）仍严重 | ❌ 已根除（#249 基线修复）。90C0 真人插话才触发 `BARGE-FIRE`，0 次回声误触发 |
| AEC 生效率仅 18%、缺陷在固件 | ⚠️ 误读。`no_candidate=73%` 绝大多数是**静音/TTS 间隙帧**（无害）；AI 播放中 AEC **生效良好**（coh=0.98，回声 357→残差 30） |

**唯一实质优化在后端（诊断指标诚实化），不在固件。详见 §5。**

---

## 1. instrument 方法（已移除）

为定位 13.6s Δt，在后端加了临时 device-scoped 调试日志（硬编码 90C0，未进 git）：
- `audioPlaybackRefMessageHandler.py`：每个 ref 到达打印 `ts=缓存key(服务端到达ms) cache=序号`。
- `connection.py _apply_aec`：每个 mic 帧打印 `ts dt matched n_cand best_ts`。

跑一轮 TTS 后读时间戳线，**分析完已 `git checkout` 还原 + 重新部署干净版（镜像 `aec-clean-20260803`）**，临时日志已确认移除（活文件 grep `AEC-DIAG-REF`/`AEC-DIAG-MIC` = 0）。

---

## 2. 证据：固件 ref 时序正确，AEC 播放中生效

**REF 流（90C0，19:07 一轮 TTS）：**
- 播放期 ref **连续发**（cache 0→463 基本连续，每 ~65ms 一帧 = 60ms TTS chunk）。
- 仅两处断流：`GAP 10788ms`（cache35→39）、`GAP 23565ms`（cache143→398）—— 即 **TTS 合成间隙**（LLM/TTS 流式分段停顿）。

**MIC 流：**
- 从 19:07:06 起**全程连续**（realtime 麦常开，已验证；非 half-duplex 切断）。

**匹配结果：**
- AI 播放中：`matched=1`，`dt=9~1800ms`，`best_ts` 落在 `[-30,+2000ms]` 窗口内 → AEC 正常对齐。
- TTS 间隙：ref 断流、mic 照常 → `dt` 涨到 8.6s、`no_candidate` 累积。

**AEC 实际质量（19:07:44-47，AI 播中 `[AEC-DIAG]`）：**
```
coh_avg=0.98          ← 参考与回声高度相关 = AEC 有效
ref_rms_avg=3169      ← AI 回声很响
residual_rms_avg≈30   vs raw_mic_rms_avg≈357~1450  ← 回声被压 ~20dB
19:07:47 mic_rms=1074(真人插话) residual=55.98  ← AEC 保留残差给 barge-in 判真人，链路正确
```

→ **固件 ref 时序正确，AEC 在播放中工作良好。**

---

## 3. 13.6s Δt / 73% no_candidate 的真因（诊断假象，非缺陷）

- `Δt = mic_ts - latest_ref_ts`。TTS 合成有间隙（10~23s），间隙内**无 ref、mic 常开** → `latest_ref` 停在间隙前 → `dt` 随 mic 推进线性涨到 8~13s。
- `no_candidate` 在此 session 聚合里（effective=334 / no_candidate=316 / mic_quiet=318）≈ `mic_quiet`：**绝大多数 no_candidate 帧是静音帧**，本就无回声可消，无害。
- 因此「no_candidate=73%」= 聆听/静默时间占比，**不代表 AEC 失效**。AI 实际播放的帧 `matched≈100%`。

**结论**：固件无需为 ref 时序做任何改动。

---

## 4. 早先文档（commit a2baa75）的错误前提 — 逐条更正

| 原说法 | 活代码/instrument 事实 | 状态 |
|---|---|---|
| 固件「收到 TTS 即发 ref、早 13-20s」 | `application.cc:1060` `OutputData` 后发；`last_output_time_`=播放时刻；ref 逐 chunk 连续；13.6s=合成间隙 | ❌ 根因说反 |
| WS 关须「彻底拆管线(de-init DMA/disarm WDT)」 | 管线常驻（Start 一次永不销毁），v1.8.5.5 已修重连崩；「拆」=回归当初崩环 | ❌ 陈旧+架构性错误处方 |
| 「转固件同事」 | 90C0 融合仓=机主自改自烧 | ❌ 角色前提错 |
| 「自激根除」与「no_candidate=73%」矛盾 | 不矛盾；73% 是静音/间隙假象，非 AEC 失败 | ⚠️ 措辞误导 |
| 麦帧 8B ts 头（Path C）是固件侧唯一落地改进 | 仍是有条件改进，但**须后端配套消费固件 ts**（当前用服务端钟；Path C 曾试因 `ref.ts` 与麦头 ts 不同钟 Δt=-2273天 回退）→ 单边移植无效 | ✅ 条件补明 |

---

## 5. 后端侧可做的（唯一实质优化 — 诊断指标诚实化）

> 红线：§5 是 `connection.py` 的**纯观测改动**，**固件侧不碰后端**，由后端 owner 落实。
> 紧急度低（#249 自激已根除、播中 AEC ~20dB 抑制正常），不阻塞任何事；「现在做」或「排期做」都行。

**反模式（务必避免）：只重贴标签。**
不要仅仅把 `no_candidate` 的分母改小（剔除静音帧）让数字好看——这会掩盖真实回归。一旦未来真出现「播放中 AEC 失效」（参考流真断、或对齐逻辑回退），被稀释的旧指标会沉默，比现在还糟。

**正确做法：拆成两个独立指标。**
- **`aec_coverage_playing`** = 仅「AI 实际播放」窗口（TTS 队列非空）内 `matched` 帧占比。
  - 这才是真指标，目标 **>90%**。任何播放中 AEC 失效都会立刻拉低它并报警。
- **`no_candidate_total`** = 保留原口径（所有 mic 帧的 no_candidate 计数），但**标注「主要由静音/间隙帧贡献」**，不再单独作为 AEC 健康度判据。

两者并存的好处：
- `aec_coverage_playing` 直接反映"AI 在说话时回声有没有被消掉"，对齐业务关注点；
- `no_candidate_total` 保留原值，便于横向对比历史、且不会因口径变更丢失趋势。

**实现要点（供后端 owner 参考）：**
- 在 `connection.py _apply_aec` 的 `no_candidate` 分支增加「当前是否 AI 播中」判断：用 `conn.audio_rate_controller.queue` 非空标识（与 `receiveAudioHandle.py` 里 `_tts_playing` 同源）；播中帧命中→累加 `aec_coverage_playing` 分子与分母，未命中→只计分母。
- `mic_quiet`（静音帧）单独计数并在 AEC-DIAG 里打印，便于区分「无回声可消」与「真漏匹配」。
- **纯观测改进，不改变 AEC 行为、不影响 #249 自激修复、不改变匹配窗口/对齐逻辑。**

**验证**：改完跑一轮 TTS，`aec_coverage_playing` 应 ≈95~100%，`no_candidate_total` 仍高但附注静音帧来源——指标不再骗人。

---

## 6. 固件侧真实可改进项（条件性，非必须）

- **麦帧 8B ts 头（Path C 风格）**：仅当后端改为消费固件 ts 才有用。前提：① 固件保证 `ref.ts` 与麦头 ts **同一时钟原点**；② 后端改 `connection.py:649` 真正用固件 ts 对齐（替代服务端到达钟）。当前两端都未满足 → 移植无收益，低优先级。

---

## 7. 后端可观测字段（自测看这些）

| 日志行 | 关注字段 | 正确信号 |
|---|---|---|
| `[AEC-DIAG]` | `coh_avg` / `residual` vs `raw_mic` | 播放中 `coh>0.95`、残差比 raw 低 >15dB = AEC 生效 |
| `[AEC-DIAG]` | `no_candidate` | 须结合 `mic_quiet` 看：仅当 `no_candidate ≫ mic_quiet` 且 AI 在播时才异常 |
| `[BARGE-DIAG]` | `rbase` / `human` / `fire` | 播放中 `human=False fire=False`，`rbase` 稳定 40~160 |
| `[BARGE-FIRE]` | `ratio` / `rbase` | 仅真人插话（`ratio` 5~25、`rbase` 40~160），无 `ratio>100` 的异常 |
| `连接关闭(WS)` | `server_initiated` | 崩环应消失（无 `server_initiated=False` 的 abrupt 断线） |

---

## 8. 相关文档

- 本机固件对接契约（hello / `audio_params` / `listen` / intercom 开关）：`docs/INTERCOM_DEVICE_INTEGRATION_CONTRACT.md`
- 对讲固件（v1.6.0）双工故障定位：`docs/INTERCOM_FULLDUPLEX_REFERENCE.md`

---

后端可配合做的对照实验：若想进一步验证「合成间隙是 Δt 唯一成因」，后端可临时把 `AEC_MAX_DELAY_MS` 窗口放大（如 30000ms）看 `no_candidate` 是否归零——若归零即坐实间隙假说（不建议常开，会误匹配错帧）。或在「退出」时改发 `session_end` 保持 WS 打开（绕过重连），但 v1.8.5.5 已修重连崩，此实验必要性低。
