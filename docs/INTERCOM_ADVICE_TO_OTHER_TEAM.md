# 8818 对讲啸叫——根因证据链与修复建议（2026-09-08）

> 收件对象：voicebox 团队（8818 固件 v1.6.0）。
> 性质：**建议**，不是结论施压。所有数据均来自后端 159 生产日志与 PCM 抓录，可复现。
> 结论先行：**8818 的 B2 上行 mic 通道（ch0）在扬声器出声时变成满幅削波墙，
> 本地 AEC 未生效；声学耦合增益约 5 倍（+14dB）。后端服务端 AEC 六种形态全部
> 实证无法补救（详见 §3），请在固件侧修复 mic 路径后我们再恢复联调。**

## 1. 现象

- 手机（小程序）与 8818 对讲：持续啸叫；手机端静音后啸叫显著减小（环路经手机腿维持的旁证）。
- 服务端侧任何 AEC/抑制参数组合均无法同时做到「不啸叫 + 双方可闻」。

## 2. 证据链（全部来自 2026-09-08 当天 159 生产实测）

### 2.1 chlvl 分通道电平（服务端 AEC `[intercom][aec][chlvl]` 诊断，5s 节流）

```
ch0（B2 契约应为"本机 AEC 后 cleaned_mic"）：
  RMS 19306~24988，峰值恒 32768 —— 满幅削波噪声墙
ch1（B2 契约回声参考 ref）：
  RMS 304~5752，峰值 23508 —— 有正常内容（参考通道是活的）
```

**要点**：喇叭驱动电平（ref/RMS≈5000）正常，但 mic 拾到的声音是其 5 倍
（RMS≈24000）且削波。把设备扬声器音量 100→30 后 mic_rms 降到 ~15000
（退出满幅削波），但啸叫依旧——mic 增益/声学耦合异常不随音量线性消失。

### 2.2 线性 AEC 全部失效（`[intercom][aec][erle]` / `[intercom][aec][diag]`）

```
ERLE（后端对消量）= 0.0dB（708 帧，mean 0.1dB，max 1.4dB）
corr(ch0, ch1) mean ≈ -0.007 ~ -0.024，strong_count = 0~4 / 708
```

**要点**：ch0 与回声参考几乎零相关。削波/啸叫自激音与参考不存在线性关系，
数学上不可对消——服务端单抽头、多抽头 NLMS、ch1 FIFO 对消三种实现同一结果。

### 2.3 六种服务端形态实测记录（供参考，避免重复试错）

| # | 形态 | 结果 |
|---|------|------|
| 1 | 2ch_srvref，NLMS+谱减，NER floor=0.1 | 不啸但手机端听设备 -20dB（≈无声） |
| 2 | 同上 floor=0.5 / 0.7 | 仍极小声（NLMS 学崩+墙残留） |
| 3 | 关 NLMS、全窗对齐 | 沙沙噪声墙传到手机（-30~40dB→部分残留） |
| 4 | 2ch 模式（ch1 活参考） | 全程啸叫，ERLE=0 |
| 5 | NER floor 0.3 / 0.1（设备腿） | 仍啸（环路经手机腿维持） |
| 6 | 双腿 NER（设备腿 0.1 × 手机腿 0.1） | 啸叫消失但音频被压至不可用 |
| 7 | 纯 B1 基线（服务端 AEC 全关，固件本机 AEC） | **啸叫更严重** |

### 2.4 PCM 抓录（服务端下行抓录，`intercom_downlink.pcm`）

24s 抓录（设备→手机 AEC 后实际到达手机的音频）：RMS 多数 <30（静音形态）或
13000+ 恒定满幅峰值（沙沙/啸叫形态），中间态不存在——mic 信号在源头上已损坏。

## 3. 后端侧已排除的原因（都有数据）

- 服务端参考错位 / 未喂参考：ref 喂入全程确认，ch1 内容正常；
- 对齐窗口/延迟带：全窗 12s 与延迟带两种设定均 ERLE=0；
- 谱减/NLMS 算法缺陷：多实现同结果，且相关性为零时任何线性方法无解；
- 手机端 bug：已单独修复一个真 bug（静音状态跨通话残留致上行全零帧），
  但与啸叫无关；手机上行音频在 09:38 通（手机静音时）实测正常。

## 4. 给固件侧的建议（按优先级）

1. **核查 8818 B2 上行 mic 通路**：对照 B2 契约，`ch0` 应为"本机 AEC 后的
   cleaned_mic"。实测为满幅削波。请确认：
   - 本地 AEC（esp_aec 或自研 NLMS）在 intercom/aec_ready 路径下是否真的在跑；
   - mic 数字增益/AGC 设置——当前拾音比扬声器驱动电平高约 14dB，远超合理范围。
2. **先修 B1 再谈 B2**：B1（mono+本机 AEC）基线实测啸叫比服务端 AEC 形态更重，
   说明本机 AEC 在对讲路径未生效或失效。修好 B1 后，后端服务端 AEC 可以整体
   退场（这是最优终态）。
3. **可复现的验证方法**：后端可随时开启 `[intercom][aec][chlvl]` 分通道电平
   诊断（5s 一条 ch0/ch1 RMS/peak），固件每改一版烧录后拨一通即可读数判定
   ch0 是否退出削波（RMS 应回到 5000 以下、峰值远离 32768）。
4. 修复前的过渡：后端可按需开启双腿 NER 强压（不啸叫但音质差，类似对讲机），
   当前 159 已恢复「压啸叫优先」形态；是否长期启用由产品定。

## 5. 后端侧同步完成的修复（不影响固件侧，已另行验证）

- `intercom_aec.py`：NER 衰减系数接配置（原为死配置恒 0.1）；phone 腿可选 NER
  （`aec_phone_residual_suppress`）；chlvl 分通道诊断。
- `intercom_registry.py`：AEC 参数透传补全；2ch 模式 ref_cache 喂入。
- 小程序：静音状态跨通话残留（UI 显示未静音但上行发全零帧）已修，
  需 DevTools 重新编译发布。

—— 后端（新枝）2026-09-08，数据可随时配合复现

---

## 6. v8 固件回应·后端侧核查（对 `v8_diagnosis.html`）

> 收到 voicebox 团队 v8 诊断（固件 fuguoai-ble-newui v8，bin `5a00d300`）。
> 结论先行：**"啸叫消失"与"双端无声"是同一个后端动作的一体两面——phone 腿 NER（环路断路器）
> 把啸叫压掉了，也把下行（手机→设备）压成了近静音。** 报告里的"后端未桥接/未投递下行"
> 被 159 日志反证；"后端 AEC 把对端语音消掉"部分成立但表述有误（详见 §6.3）。

### 6.1 后端侧可复现证据（159 生产日志，session 含 phone-Uc7d7cd08de20 ↔ 94a9901a8818）

```
[intercom][debug] 手机→设备[pcm] phone-Uc7d7cd08de20 in_len=640 pcm_chunks=1 opus_out=51 frames=102
[intercom][debug] 设备→手机[pcm] phone-Uc7d7cd08de20 opus_len=134 pcm_len=1920 frames=24
[intercom][debug] 手机→设备[pcm] ... opus_out=48/54/52 ...   ← 手机上行在发，且非全零
[intercom][debug] 设备→手机[pcm] ... opus_len=108/136 ...     ← 设备上行已转发给手机
```

→ **relay 双向桥接正常**：手机→设备、设备→手机都转发，opus 字节数非零。
报告 claim #1（"设备下行 92% 静音 = 后端没投递"）与 claim #3（"后端未把设备上行转发给手机"）
**被日志直接反证**。问题不在"桥接有无"，在"下行内容音量"。

### 6.2 "啸叫消失"是真——而且正是后端 phone-NER 的功劳

159 **v8 当时**配置（本轮已变，活配置见 §6.8）：`aec_phone_residual_suppress: true`、
`aec_residual_floor: 0.1`、`server_aec_mode: "2ch"`、`server_aec_devices: [94a9901a8818]`。
phone 腿 NER（`intercom_aec.py` NER 分支 :423-440，衰减施加 :748）把手机上行
（= 设备下行）在"远端(设备)在讲"时按 `residual_floor=0.1` 衰减 −20dB，
硬把"手机→设备→手机"声学环的增益压到 <1，彻底断环。报告测到 howling 消失，
与这一后端动作一致，非纯固件功劳。

### 6.3 "device 下行近静音"根因 = 后端 phone-NER（后端可控，已定位）

代码路径（`intercom_aec.py`）：
- phone 上行走 `process_ref_cache` 的 `outband and self._ner_outband` 分支（:423）。
- `ref_active` 门控：device 在 `[mic_ts-300, mic_ts]` 内发过帧即激活（:728-740），
  并带 `_ref_active_hold_ms=1500` 拖尾（:157, :435）。
- device 在通话中基本每 ~20ms 发一帧 → `ref_active` 近常驻 → phone 上行**整通被 ×0.1**。
- 衰减施加：`_apply_residual_suppress` 在 `ref_active` 时 `out *= 0.1`（:521-526）。
- 该 phone 上行经 `_transcode` 后即成**设备下行**（`intercom_registry.py` :375→:405）。

→ 报告 §2 的 `ch1_rms<300`（设备下行近静音）**后端侧解释成立**：手机发来的是真声，
但被后端 phone-NER 压成 1/10 才送达设备。这对应"双端无声"的设备侧那一半。

**表述纠正（非杠）**：报告 claim #2 说"server-side AEC 把对端语音误判为回声消掉"——
方向上对（确实是后端把 phone 语音压了），但机制写错：不是 device-uplink AEC 碰了下行
（device 上行在 2ch 模式**有 NER**：`intercom_aec.py` :733-748，门控=device 下行=手机在讲，
衰减 ×`aec_residual_floor`；:421 仅 `2ch_srvref`/outband 分支，是另一条路径），
而是 **phone 腿 NER + device 腿 NER** 共同衰减了对应近端的语音，且两条腿在双讲时都触发。
device 腿 NER :733-748 即"设备→手机"在手机讲话时被 ×0.1 压掉的根因（见 §6.4 实测）。

### 6.4 "phone 听不到 device" 那一半——后端无责，需 phone 侧确认

device→phone 在 2ch 模式**施加 device 腿 NER**（:733-748，门控=device 下行=手机在讲，
`aec_residual_suppress` 控制，`aec_residual_floor`=0.1 → ×0.1）。日志 `opus_len=108~136`
证明后端已把设备上行转发给手机，但转发的是**被 NER 压过的内容**——这才是"手机听不到设备"的根因。
若手机仍听不到设备（已关 device NER 后仍无声）：
- 大概率 phone 小程序**播放侧**未把收到的设备音频送扬声器（报告 checklist #4 只查了
  capture/麦克风权限，没查 playback 路由）；
- 或 device 2ch AEC 输出过轻（device 上行 ch0 经后端谱减后级）。
需 phone 侧补一条"收到的设备音频 RMS / 是否真在扬声器出声"的仪表，才能定位。

### 6.5 修复提案（后端可控部分）

1. **立刻实验（不固化，等复拨验收）**：
   - `aec_phone_residual_floor` 0.1 → **0.3**：环路增益 = α(0.3~0.7)×0.3 ≈ 0.09~0.21 仍 <1 断环，
     但下行少 10dB 衰减，设备可闻度显著回升。
   - 把 NER 门控从"device 发了帧"改为"**device 下行 RMS ≥ 阈值**"（真有回声能量才压），
     避免静默/小声帧也把 phone 真声压掉（双讲更自然）。
2. **加后端 downlink RMS 仪表**：phone uplink PCM RMS（NER 前/后）+ device downlink RMS
   落盘/节流打点，与固件 `ch0/ch1_rms` 在**同一通**对齐，做受控复拨
   （报告 scenario A/B 思路正确，但必须两端同时有仪表才有意义）。
3. **根因修复（推荐架构）**：小程序做本机声学 AEC（Web Audio AECM），后端 phone-NER
   即可整体关闭 → 下行满幅、无环、无双讲闷。这是治本，但需小程序排期。

### 6.5b 已部署修正（2026-09-09，修复"设备→手机基本没声音"）

- 根因定位：`intercom_aec.py` :733-748 的 **device 腿 NER** 在 2ch 模式下对设备上行
  施加 ×0.1（门控=device 下行=手机在讲）→ "设备→手机"在双讲时被压到近静音。
- 修正：159 config `aec_residual_suppress: true → false`（关 device 腿 NER）。
  环路仍被 phone-NER(0.3) 兜死（最坏增益 ≤0.3<1，不啸叫），但手机听设备恢复正常电平。
- **临时过渡形态（非终态）**：`server_aec_mode=2ch` / `aec_residual_suppress=false`
  （device 腿 NER 关）/ `aec_phone_residual_suppress=true` + `aec_phone_residual_floor=0.3`
  （phone 腿 NER 开、−10dB）。⚠️ 固件重烧新版本后**间歇啸叫回归 + 设备→小程序基本杂音/小声**
  （根因见 §6.8）——此形态已被证伪，不能定版。

### 6.6 待 voicebox 团队回确认认的点

- phone 小程序播放侧是否真的在扬声器出声（对应 §6.4）；
- `aec_phone_residual_floor=0.3` 已部署（§6.5-1 部分落地）；RMS 门控为代码内置
  （`intercom_aec.py:738` `_REF_RMS_FLOOR`），真正根治见 §6.8 的 `2ch → 2ch_srvref` 模式切换提案；
- v8 的 `ch0_rms=993` 是"AEC 后"电平，对原始 mic 是否仍削波无法直接反证我们 §2.1 的
  削波墙结论——建议保留 `[intercom][aec][chlvl]` 诊断，复拨时一并读数。

—— 后端（新枝）核查，证据锚点（均经本轮读源码核实，非记忆）：
`intercom_aec.py` :704-715（2ch 用固件 ch1 作 ref）/ :733-748（2ch device 腿 NER）/ :421（2ch_srvref device 腿 NER）/ :754-769（丢弃坏 ch1 取 ch0）/ :781 + :788-817（`_process_mono_aec` 子帧对齐修复）/ :541（2ch_srvref corr 恒 -1 病）；
`intercom_registry.py` :686-715（`_feed_intercom_ref` 服务端自喂下行 ref）/ :727-729（ref_cache 三模式可用）/ :753（phone 腿 ref）/ :185（`_aec_modes`）；
159 活配置 `server_aec_mode`/`aec_residual_suppress`/`aec_phone_residual_floor`；容器 intercom_aec.py md5 `7b6ce77b` ≠ 本地修正版 `d831acba`（缺子帧修复）。

### 6.8 最新现状复核与根治提案（固件重烧后 · 本轮读源码核实）

> 用户要求"再给文件时检查清楚"——本节修正前几节残留的不一致，并给出**经源码核实**的根治方向，
> 不再用 NER 折中打补丁。

**① 159 活配置（本轮 `cat` 核对，非记忆）**
```
server_aec: true
server_aec_mode: "2ch"            ← 当前走 2ch（依赖固件 ch1 作回声参考）
server_aec_devices: [94a9901a8818]
aec_residual_suppress: false      ← 11:06 关 device 腿 NER（修复"设备→手机没声音"）
aec_residual_floor: 0.1
aec_phone_residual_suppress: true
aec_phone_residual_floor: 0.3     ← phone 腿 NER −10dB 兜底（已部署）
```

**② 固件重烧后现象（用户反馈）**：间歇啸叫回归；设备→小程序基本是杂音、声音很小。

**③ 根因（读源码，非记忆猜测）**
- 当前 159 跑 `2ch`：`intercom_aec.py:704-715` 设备腿 AEC **拿固件 ch1 当回声参考**。
  但固件 ch1 与 mic(ch0) 实测零相关（corr mean≈-0.001、strong_count=0、ERLE=0.0dB）→
  线性对消"无米下锅"，设备腿 AEC **零抑制**。
- 关 device 腿 NER（`:733-748`）后：设备→手机电平恢复，但环路**只在 phone 腿 NER(0.3) 单侧断**。
  新固件耦合更强 → 单侧断环不足 → 间歇啸叫回归。这是 NER 折中的**必然两难**，不是配置错。

**④ 根治（非 NER 补丁）：`server_aec_mode: 2ch → 2ch_srvref`**
- `2ch_srvref` 丢弃坏 ch1（`intercom_aec.py:754` / `mic_all = mic_all[0::2]` :769），改走
  **服务端自喂下行 ref**（`intercom_registry.py:_feed_intercom_ref` :686 + `:781` `_process_mono_aec`）——
  即"设备喇叭实际在播的手机声音"= 真回声源，与固件 ch1 好坏无关。
- 本地仓 `intercom_aec.py`（md5 `d831acba`）已含**子帧对齐修复**（`:788-817` `_process_mono_aec`）：
  把任意帧长上行切成 320 子帧对齐，互相关生效 → 2ch_srvref 的 ERLE **可 >0**，设备腿盲 NER
  由 `aec_residual_suppress` 控制（`:421`），可保持关，设备→手机真声保留。
- **设备无需改固件**（仍发 2ch B2）；phone 腿 NER(0.3) 继续兜底。
- ⚠️ **159 当前容器 `intercom_aec.py`（md5 `7b6ce77b`）缺子帧修复**，其 2ch_srvref 的 AEC 仍
  `corr 恒 -1`（`:541` 注释），**直接切 mode 无效**——必须先部署本地修正版（d831acba）再切 mode。

**⑤ 待用户授权后执行（守先别固）**
1. 部署本地修正版 `intercom_aec.py`（d831acba）+ 已改的 `helloHandle.py` / `intercom_registry.py`
   （SCP+docker cp，清 __pycache__，restart，md5 零差异核对）。
2. 159 config：`server_aec_mode: "2ch" → "2ch_srvref"`，保持 `aec_residual_suppress: false`、
   `aec_phone_residual_floor: 0.3`。
3. 复拨验收：`[intercom][aec][erle]` 出现 ERLE>0（非 0.0dB）、无间歇啸叫、双端可闻 → 定版。
4. 若 AEC 单独不够断环，再把 `aec_residual_suppress` 开 true（device 腿 NER 兜底，双讲时设备→手机
   衰减 −10dB 仍可接受），不回到"双端全灭"的旧态。

—— 本轮（固件重烧后）后端（新枝）复核

### 6.9 后端部署执行记录（已部署 · 待复拨验收）

> 用户授权"后端的问题及时改"，固件侧暂停重烧。按部署铁律执行，守先别固（未 git/docker commit，待验收）。

- 部署：`intercom_aec.py` 本地修正版（md5 `d831acba`）SCP→docker cp→清 __pycache__→restart。
- 配置：`server_aec_mode: "2ch" → "2ch_srvref"`（host bind mount `.config.yaml`），保持
  `aec_residual_suppress: false` + `aec_phone_residual_floor: 0.3`。
- 核实：`/metrics` 首探 200（healthy）；容器活文件 md5 = `d831acba` 零差异；启动无 traceback；
  2ch_srvref 路径已入容器（grep 命中 23）。
- 待用户复拨：拉 `[intercom][aec][erle]` 看 ERLE 是否 >0（非 0.0dB）、是否间歇啸叫、双端是否可闻。
  - ERLE>0 + 不啸 + 双端可闻 → 定版（git commit + docker commit `fuguoai-server:intercom-2chsrvref-20260909`）。
  - 仍间歇啸叫（AEC 单独不够断环）→ 开 `aec_residual_suppress: true`（device 腿 NER 兜底，双讲设备→手机 −10dB 仍可接受），不回"双端全灭"。
- 注：registry/helloHandle 容器版已具备 2ch_srvref 路由与 ref 自喂，本次未重发（最小爆炸半径）。

### 6.10 复拨实测结果（2ch_srvref · 2026-09-09 12:16 通话）

**① 模式切换生效**：`[intercom][aec] 服务端 AEC 已启用 device_id=94a9901a8818 mode=2ch_srvref`；phone `mode=outband`。

**② 子帧修复生效（机制通了）**：`uplink_pcm_samples=960 (=60ms)` 已切成 3 个 320 子帧对齐；
回声延迟诊断不再钉 -1：
```
[intercom][aec][diag] mode=2ch_srvref -> samples=2365
  delay_ms:        min=2096 max=7092 mean=4509.7 median=4493 stdev=1453.1
  corr:            mean=0.264 min=0.016 max=0.862 strong_count=25 weak_count=2340
  strong_delay_ms: min=2315 max=6753 mean=4757.5 median=5004 stdev=1258.6
  ref_active_rate: 0.997
```
→ **corr 恒 -1 的病已消除**（强相关帧 0 → 25，峰值 0.862），服务端自喂 ref 的对齐机制恢复正常。

**③ 但仍未达标（用户复拨反馈：小程序端杂音很重）**
- 强相关帧仅 **25/2365 ≈ 1%**，corr mean 仅 0.264 → **AEC 未锁住**，抑制量不足。
- **延迟估计 ~4.5s、抖动 ±1.45s（2.1~7.1s）**：任何线性 AEC（单抽头/多抽头/NLMS）都追不上这个
  量级的延迟与抖动 → 数学上无法收敛。
- 注：`[intercom][aec][erle]` 在 2ch_srvref 分支**不打印**（`_record_erle` 仅 2ch 分支 :726 调用），
  本轮无法用 ERLE 量化抑制量——待补埋点后可直接读数。

**④ 结论：后端这侧已做到位，剩余阻塞在设备端音频管线延迟（~4.5s、抖动 ±1.45s）**
- 服务端只能测到「下行发出 → mic 捕获」的总延迟，**无法区分是播放缓冲还是上行缓冲**，需固件定位。
  与 90C0 已知的「扬声器出声 5~9s」属同一类问题。
- **环路自持**：`ref_active_rate=0.997` 说明设备下行（=手机上行）几乎恒有内容——设备播手机声 → mic
  拾回 → 送手机 → 手机再播 → …… 多秒延迟下这个环永不衰减，这正是「杂音」的来源（手机听到的是
  自己声音被延迟数秒回送的回声 + 谱减残留，不是设备人声）。
- **后端 NER 兜底此处是劣质选择**：门控近常驻（0.997），开 device NER 会把设备→手机**整通 ×0.3 压小**
  （回到「基本没声音」旧态），关则回声/噪声重——两难依旧，NER 折中到此为止。

**⑤ 给固件的定位请求（阻塞项，建议优先于其他改动）**
1. 量设备端音频管线**分段**延迟：① 下行到喇叭出声 ② mic 采集到上行发出。
   目标：**端到端 < 200ms 且抖动 < 20ms**（现 ~4.5s / ±1.45s）。
2. 若是**播放缓冲**过大 → 缩小播放 buffer（对讲场景不要为流畅而深缓冲）。
3. 若是**上行缓冲**过大 → 上行按 20/60ms 帧即时发送，不攒帧。
   （用户此前反馈「设备端播放还好」，倾向于上行攒帧侧，但需固件实测定位，后端无法区分。）

---

## 7. 固件具体修复清单（给 voicebox 团队 · 2026-09-09）

> 目标：让 8818 在**不依赖后端 phone-NER** 的情况下也干净——那样后端可把 phone-NER
> 进一步降（甚至关），逼近真全双工。当前 159 已先按 `aec_phone_residual_floor=0.3` 让
> 设备侧可闻（对讲机形态可用），但这是后端兜底，不是终态。

### 7.1 责任边界（先讲清，免返工）

| 回声腿 | 谁负责消除 | 现状 | 说明 |
|---|---|---|---|
| **设备腿**（设备喇叭→设备 mic） | **固件**本地 AEC | v8 HOWL-FFT 压住啸叫，但原始 mic 耦合仍异常 | 本节 §7.2/7.3 固件修 |
| **手机腿**（手机喇叭→手机 mic） | **小程序**本机 AEC | 微信 `RecorderManager` 无 echoCancellation，无 AEC | **非固件责任**；我们小程序 backlog（§7.4） |

→ 手机腿回声只能靠小程序或后端 phone-NER 压，**固件改不动手机硬件**。所以"双端无声"里
手机听不到设备的那一半（§6.4）若不是播放路由 bug，就是 device 2ch AEC 输出过轻，归固件 §7.3。

### 7.2 核查原始 mic 路径是否仍削波（最高优先级）

v8 报告的 `ch0_rms=993` 是 **"AEC 后"电平**，不能证明原始 mic 干净。我们 §2.1 实测
**原始 mic RMS≈24000、峰值恒 32768（满幅削波）**，且比扬声器驱动电平高约 **5×(+14dB)**。
请固件在同一通额外开 **AEC 前原始 mic RMS** 诊断（不要只看 AEC 后），对比扬声器驱动电平：
- 验收标准：扬声器出声时 **原始 mic RMS 远离 32768**（退出满幅削波）；
- mic / speaker 电平比 **≤ 1.5×**（现约 5×，严重超标）。

### 7.3 固件侧具体动作（按优先级）

1. **降 mic 数字增益 / 重设 AGC target**：把 mic 增益调到满音量也不削波。
   当前 +14dB 过量增益是回声在源头被放大的根。改完用 §7.2 诊断读数确认。
2. **确认本地 AEC 在对讲/aec_ready 路径真正收敛**：
   - 实例在 `intercom` 激活且 `aec_ready=true` 时确实在跑（不是空转）；
   - 参考信号 = 设备自身扬声器输出（audio_playback_ref / 喇叭 PCM），不是别的；
   - 收敛后设备上行 ch0 在"扬声器出声 + 人在讲"时仍保留近端语音、且回声残留低。
   - 验收：设备上行 ch0 经后端 2ch 谱减后 **ERLE≥15dB**、且近端语音可闻（不需后端 NER）。
3. **HOWL-FFT 是啸叫抑制器，不是回声消除器**：它压住啸叫但没解决源头耦合。
   §7.2/7.3 是从源头降回声，能显著改善双讲自然度与"手机听设备"的音量。
4. **复现验证法（每版烧录后拨一通）**：开后端 `[intercom][aec][chlvl]`（5s 一条
   ch0/ch1 RMS/peak）+ 你们侧**原始 mic RMS**。读数判定：ch0 退出削波 + 设备→手机可闻
   （此时后端把 `aec_phone_residual_floor` 降到 0.1 甚至关 phone-NER 也应不啸）。

### 7.4 小程序侧（我们的 backlog，不阻塞当前"可用"，列出供同步认知）

真全双工需手机腿回声在手机端消除。两条路（我们排期，非固件）：
- **A（推荐）**：对讲音频改走微信 `live-pusher`/`live-player` WebRTC 通路（自带 AEC），
  重写采集/播放；收益=后端 phone-NER 可整体关闭。
- **B**：在 `WebAudioContext` 播放回路里跑 NLMS AEC（JS 实现，参考=播放的设备音频），
  工程量大、时延敏感，次选。

当前（2026-09-09）已部署：`aec_phone_residual_floor=0.3`（对讲机形态可用），待复拨验收。

—— 后端（新枝）2026-09-09，固件清单供 voicebox 团队执行

---

## 8. 2026-09-10 复拨实证：「手机没声音」归属判定 + 一处死配置更正

> 触发：用户反馈「啸叫没有了，小程序端还是没声音」。本节全部结论均来自**当日两通真实通话**
> 的服务端读数与容器活代码，非记忆、非推断。

### 8.1 两通复拨的实测对比（同日，同一设备 94a9901a8818，mode=2ch_srvref）

| 项 | 通话① 13:54:15→13:55:12 | 通话② 14:12:47→14:14:01 |
|---|---|---|
| `chlvl` ch0(mic) | **RMS 0~2、peak ≤17（全程数字死）** | **RMS 32~2848、peak 105~7943（活）** |
| `chlvl` ch1(ref) | RMS 105~5446、peak 22246 | RMS 87~7005、peak 23859 |
| 设备→手机 post-AEC opus（60ms/帧） | 中位 **22B**、均值 34、**90% 帧 <60B**＝静音包 | 中位 **113B**、均值 120、**仅 3% <60B**＝真实音频(≈15kbps) |
| 手机→设备上行 | 640B/20ms 连续（正常） | 640B/20ms 连续（正常） |

### 8.2 结论一：通话①「手机没声音」= 设备上行 ch0 无信号，**不是后端 AEC 造成**

- 后端侧 `intercom_downlink.pcm`（**AEC 之后**、发手机的那份，写入点
  `intercom_registry.py:825-841`，链路顺序 `:374` AEC → `:375` transcode）实测
  **99.8% 帧静音、RMS 3.7(−78.9dBFS)、峰值 176**，与上表「中位 22B 静音包」互为印证。
- 代码级反证 AEC **无法**产生这种静音：`intercom_aec.py:336`（`process_ref_cache`）与
  `:563`（`_apply_residual_suppress`）在 `mic_rms < _MIC_RMS_FLOOR(30)` 时**原样返回**；
  ch0 全 0 → 输出必为全 0。AEC 不会、也不能凭空造声或消灭本不存在的声。
- 机制自洽：设备 mic 数字死 ⇒ ①设备→手机无内容（手机没声音）；②扬声器→mic 声学环
  **不存在** ⇒ 反馈环断裂 ⇒ **啸叫自然消失**。用户报告的「不啸了 + 没声音」是**同一个原因**。
- 归属：**设备侧**（mic 采集/AEC/AGC 把 ch0 钳到数字零），非后端。后端对讲链路
  （relay 双向、AEC 透传、格式、帧率）本通全部正常。

### 8.3 结论二：通话② ch0 已恢复，后端正在下发真实音频

- ch0 由「全 0」变为「有近端人声」；且 ch1(ref) 大声时 ch0 反而低（ch1=7005→ch0=42）
  ⇒ 设备本地 AEC 在工作、ch0 装的是近端语音而非自身回声。
- 后端下发手机的中位 opus 113B/60ms ⇒ **下行有内容**。若用户在此状态下仍「没声音」，
  断点在后端**之后**（小程序播放侧/手机音频路由），需按 8.5 在小程序侧取数定位。

### 8.4 🔴 死配置更正：`aec_phone_residual_suppress` 从未生效（此前文档描述有误）

- 159 `data/.config.yaml` 中该行**顶格写在 `intercom:` 之外**（root 层），且——
- **代码里没有任何一处读取该键**：`grep -rn "aec_phone_residual_suppress|ner_outband"` 全库仅命中
  **注释/文档**；唯一入口 `helloHandle.py:278-284`（phone 腿）根本没传 `ner_outband`，
  `intercom_registry.py:594` 的形参默认 False → `intercom_aec.py:95` `_ner_outband=False` 恒定。
  容器活代码 grep 结果与本地一致（已实测，非仅读本地）。
- **影响**：`intercom_aec.py:423` 的 phone 腿 NER 分支永不进入 ⇒ §6.9/§6.10 所述
  「环路被 phone-NER(0.3) 单侧断死」**不成立**；当前 **device 腿 NER 关
  (`aec_residual_suppress:false`) + phone 腿 NER 实为关** ⇒ **两端 NER 全关**。
  当前不啸叫靠的是 AEC 线性对消 + 通话①的设备 mic 死（§8.2），**没有 NER 兜底**。
- 处置（待用户拍板，均需改配置或代码，未执行）：
  - 选项甲：把该键移入 `intercom:` **并**在 `helloHandle.py` phone 分支补传
    `ner_outband=_icfg.get("aec_phone_residual_suppress", False)` → phone 腿 NER 真正生效
    （代价：双讲时手机→设备 −10dB）。
  - 选项乙：删掉该死配置行，避免后续再按「有 NER 兜底」误判（文档同步更正）。

### 8.5 阻塞项与下一步取证

- **延迟阻塞未改善**：通话② diag `delay mean=4344 median=4272 stdev=1476`，与 09-09 的
  `4509/4493/1453` 同量级 ⇒ §6.10 ④ 给固件的定位请求（端到端 <200ms、抖动 <20ms）**依然成立**。
  单帧线性 AEC 在 ±1.5s 抖动下无法稳定对齐，ERLE 无法达标。
- **已布防取证**：容器 `/opt/fuguoai-server/data/tmp/.intercom_pair`（本轮新 arm）→ 下次复拨落盘
  `intercom_pair.pcm` = 3ch 交织 `[mic, ref, out]`（16k，magic `INTCPAIR`，mode_code=1）；
  可离线算 **2ch_srvref 分支的 ERLE**（该分支 `_record_erle` 不打印，见 `:726` 仅 2ch 调用）
  并验证 out 是否含设备人声。上限 8MB(≈83s)。
  ⚠️ `intercom_downlink.pcm` 的 400 帧配额已用尽（`_dump_n` 状态不随会话重置），本轮已将
  `.intercom_dump` 摘除以复位，需要 post-AEC PCM 时再 arm。
- **小程序侧自查（信息性，非后端责任）**：复拨时看小程序 console
  `[IntercomService] ↓ 下行 PCM RMS=`（阈值≈0.003）：非零=收到了有声内容→断点在播放/路由；
  ≈0 或整行不出现=没收到或解码被静默替换。另注 `libs/opus/opus-decoder.js:81`
  对 **<200B 帧直接返回 `silentPcm()`**（只警告一次）：当前下行是裸 PCM(1920B/60ms) 不受影响，
  但**一旦手机腿改真 Opus，手机会静音播放且几乎无提示**——留给后续排期留意。

### 8.6 文件一致性核对（本轮实测）

| 文件 | 容器活文件 md5 | 本地工作区 md5 | 结论 |
|---|---|---|---|
| `intercom_aec.py` | `4644d046…` | `4644d046…` | ✅ 零差异 |
| `intercom_registry.py` | `43e1b8bc…` | `43e1b8bc…` | ✅ 零差异 |
| `helloHandle.py` | `79171448…` | `79171448…` | ✅ 零差异 |

- 本地 `git HEAD = 881ec6e`；`git diff --stat` 对 `intercom_aec.py` 仅 **+44 行**
  （即 `_maybe_capture_pair` 诊断）⇒ **「子帧修复」已在 git HEAD**，未提交部分仅诊断代码。
- 更正：§6.8 ⑤ / §6.9 所记容器 md5 `d831acba`、`7b6ce77b` 已过期，以本表为准（守先别固：仍未 commit）。

—— 后端（新枝）2026-09-10，本轮读活代码 + 两通真机读数

---

## 9. 2026-09-10 二次复盘：「声音很小 + 杂音」真因在后端 NER 硬编码，非固件

**触发**：voicebox 已部署 `kHfHoldFrames` 187→8（SHA256 `e9549423…`）后，手机端症状仍为
「声音很小 + 基本是杂音」，固件侧仍在继续改。**本节结论：这不是固件的 bug，请先停止在
`HowlFftProcess` / 抑制器上继续找「声音小」的原因。**

### 9.1 🔴 核心发现：设备腿 NER 固定 ×0.1，且任何 config 都关不掉

| 环节 | 位置 | 事实 |
|---|---|---|
| 设备腿创建 AEC | `helloHandle.py:320-326` | 调用 `enable_intercom_aec(...)` **未传** `residual_suppress` / `residual_floor` |
| → 吃默认值 | `intercom_registry.py:592-593` | `residual_suppress=True`、`residual_floor=0.1` |
| → 分支命中 | `intercom_aec.py:421-422` | `mode=="2ch_srvref"` → 无条件调 `_apply_residual_suppress` |
| → 实际衰减 | `intercom_aec.py:567-569` | `out_f = out_f * atten`，`atten = residual_floor = 0.1` ⇒ **上行 −20 dB** |

**门控条件极宽**：`aec_max_delay_ms=12000`（`_hi=12000`）、`aec_echo_delay_ms=0`（未收窄，
`intercom_aec.py:348-356` 保持 `[-30, 12000]` 全窗口）→ 只要**过去 12 秒内**有任一 ref 帧
RMS≥50 即 `ref_active=True`，再加 **1500 ms hangover**（`:157` / `:377-378`）。

### 9.2 159 生产实测（本轮 3 次 `[intercom][aec][diag]` 全样本）

| samples | delay mean | delay median | delay stdev | corr.mean | strong | weak | **ref_active_rate** |
|---|---|---|---|---|---|---|---|
| 108 | 4232.3 | 3880 | 1400.8 | 0.294 | 2 | 106 | **0.982** |
| 271 | 4238.1 | 4083 | 1464.0 | 0.299 | 20 | 251 | **0.996** |
| 215 | 4569.9 | 4494 | 1556.4 | 0.253 | 1 | 214 | **1.000** |

- `ref_active_rate = 0.982~1.000` ⇒ **NER 门控 98~100% 的帧都在 fire** ⇒ 设备上行几乎
  **全程 −20 dB**。这就是「声音很小」的直接量化证据，与固件无关。
- `stdev` 1400~1556 ms（不是 mean 大，是**抖**）：在 600 个候选帧里 95%+ 选到错参考帧
  （`strong_count` 仅 1~20 / `weak_count` 214~251，`corr.mean` 0.253~0.299）。
  ⇒ **啸叫消失靠的完全是 NER 的 ×0.1（不依赖对齐），不是线性 AEC 的功劳。**
- 副证：小程序 `libs/audio-player.js:199/234` 固定 `volume=1.0`、**无 AGC** ⇒ 不存在
  「被下游放大」的解释，−20 dB 是实打实的、没有任何补偿。

### 9.3 🔴 三处 config 是死键（写了没人读，会误导判断）

| key | 所在段 | 声明含义 | 实际 |
|---|---|---|---|
| `aec_residual_suppress: false` | `fuguoai.intercom` | 关设备腿 NER | 🔴 无人读 → 实际 **True（开着）** |
| `aec_residual_floor: 0.1` | `fuguoai.intercom` | 设备腿地板 | 🔴 无人读 → 默认**恰好也是 0.1**（制造「生效了」的假象） |
| `aec_phone_residual_suppress: true` | **root** | 开 phone 腿 NER | 🔴 无人读 → phone 腿 NER 实际 **关**（`ner_outband` 无人传，`:423` 分支不可达） |
| `aec_phone_residual_floor: 0.3` | `fuguoai.intercom` | phone 地板 | ✅ 真被读（`helloHandle.py:277`）但 phone NER 关着 → 白读 |

- 设备腿**真正**被读到的只有：`aec_max_delay_ms` / `aec_echo_gain_cap` / `aec_echo_delay_ms` /
  `aec_echo_delay_band_ms` / `intercom_aec_diag` / `aec_multi_tap` / `aec_tap_len_ms` /
  `aec_nlms_mu` / `aec_nlms_w_cap` / `aec_dtd_ratio`（`helloHandle.py:308-319`）。
- `aec_echo_gain_cap` **不在 config** ⇒ 用代码默认 0.8。
- 更正 §6.9/§6.10：此前「phone-NER 断环」的叙述不成立——phone 腿 NER 从未开过。

### 9.4 调整方案（按可落地成本排序）

| 步 | 动作 | 成本 | 预期 |
|---|---|---|---|
| 0 | **判决实验**：手机端静音 10s，只让设备端说话 | 零 | `ref_active` 1.5s 后落 → NER 停 → **声音立刻恢复正常** ⇒ 石锤 |
| 1 | `helloHandle.py:320-326` 补 `residual_suppress`/`residual_floor` 两参（照抄 phone 腿 `:277` 写法），config 真能关；再 `aec_residual_floor 0.1→0.5`（−6 dB） | 2 行 + 改配置 | 「小」回来 ~5×，「啸叫」不回来 = 两端都活 |
| 2 | config-only：`aec_echo_delay_ms 0→4300`（band 2500 保持） | 零 | 候选窗 12030→5000 ms，减半误锁 → 若「杂音」来自错参考谱减会明显改善 |
| 3 | 真治本：固件 **line-echo-canceller**（voicebox 已提，方向正确） | 固件侧 | 唯一能解开「后端靠 NER 硬压」的路 |

- 步骤 1 的**默认值必须取 `True`/`0.1`**（`_icfg.get(key, <旧默认>)`）⇒ 键缺失时行为与现状
  完全等价，零回归；只是把「关不掉」变成「可关可调」。
- **不要**现在直接 `aec_residual_suppress=false`（=atten 1.0）——啸叫会立刻回来。走 0.1→0.5→0.7 渐进。
- **不要**现在上 `aec_multi_tap=true`：NLMS 前置要求 bulk delay 稳定，stdev 1.4 s 下 FIR 基准
  每帧跳，学不到东西反而新增不稳定。

### 9.5 固件侧 chlvl 现状（信息性，不构成结论）

最新 6 帧：`ch0_rms=7~45 / ch1_rms=212~814`（ch0_peak 27~175 / ch1_peak 1087~2908）。
ch1（设备播放参考）活跃、ch0（mic）静 —— 但**不能据此判 ch0 死**，因为这些帧很可能没人对着
设备说话。要判定必须在**设备端有人说话时**抓 `chlvl`。
另注：`ch0_rms < _MIC_RMS_FLOOR(30)` 时 `intercom_aec.py:336` 直接 `return mic_pcm` 跳过 AEC
⇒ 这些帧后端**未做任何处理、原样转发**。

—— 后端（新枝）2026-09-10，读活代码 + 3 次 diag 全样本 + config 实测

### 9.6 已部署到 159（2026-09-10 15:11，未 commit，守先别固）

按 §9.4 步骤 1 落地：
- `helloHandle.py:319-332`（设备腿）补 2 行传参，把 `aec_residual_suppress` / `aec_residual_floor`
  **真正接入** config，默认值 `True`/`0.1` 与旧行为完全等价（零回归）。
- 159 config：`aec_residual_suppress false→true`（保持 NER 开，否则会被脚注里的新代码读成关闭→啸叫回）、
  `aec_residual_floor 0.1→0.3`（−20dB→−10.5dB，约 3× 音量）。
- 部署：scp → `docker cp` → 清 `handle/__pycache__` → restart → `/metrics` 200；
  容器活文件 md5 `d30f6515…` == 本地。
- **未 git commit / 未 docker commit**（守先别固，等听感确认）。步骤 2（`aec_echo_delay_ms 0→4300`）暂未动，
  待本次听感反馈后再单变量推进。

—— 后端（新枝）2026-09-10 15:11 部署记录

### 9.7 步骤 1 实测：后端只占了「小」的一小部分，主因在设备上行

**时间线**：15:11 部署 `aec_residual_floor 0.1→0.3`（−20→−10.5 dB，约 3× 音量）。
15:14~15:16 用户复拨，回报「**还是很小，有杂音**」。

**排查（拉 159 日志）**：
- 设备腿确为 `mode=2ch_srvref`（`94a9901a8818`，非回退 outband），NER 仍在工作
  （diag `ref_active_rate=0.997`）；代码 md5 `d30f6515…`= 容器内活文件 ⇒ **步骤 1 确实生效**。
- 但 `chlvl`（AEC 之前、设备上行原始 mic）实测：`ch0_rms=4~96，ch0_peak=16~598`。
- 对照**能正常通话那次（session ②）的 chlvl**：`ch0_rms=32~2848，ch0_peak=7943`。
- ⇒ **同样对着设备说话，这次上行 mic 信号比可用基线低约 13 倍**（peak 598 vs 7943）。

**结论**：
1. 「声音很小」**主因在设备上行那一头**（设备 mic 捕获 / 固件上行处理），后端 NER 只占其中
   ~3× 的一档（已从 −20 dB 松到 −10.5 dB，但盖不住 13× 的上游落差）。
2. 「有杂音」与低电平一致：被砍到 ~peak 180 的语音再过 24kbps opus + NER 帧切换泵动 + 残留回声，
   噪声底占比天然高。后端再松 NER 也救不了上游 13× 落差。
3. 这与 voicebox 自己定位的方向一致（`kHfHoldFrames` 抑制器门闩压话），但 187→8 只缓解、
   **上行 mic 电平仍未回到基线**——请回到固件 uplink 增益 / 抑制器仍压话的那一段。

**待量化**：已 arm `/tmp/.intercom_dump`，请用户再拨一通、对着设备说话 5s，
抓 `intercom_downlink.pcm`（= 手机真正收到的波形）离线测 RMS，给出「手机收到 peak=X / dBFS=Y」硬数。

—— 后端（新枝）2026-09-10 15:21

> **§9.7 更正（15:27）**：上条「ch0 低 13×」是被回声处理污染的**假象**——那次 chlvl 在你开麦、回声路径激活时测，下行正崩成 hiss 故 rms 低。
> 用户实测「**静音后能听清对端**」证明设备上行健康。真正结论见 §9.8。

### 9.8 静音测试 + 下行 PCM 量化：真因是双讲回声处理把链路打成 hiss

**判决实验（用户 15:27 自测）**：小程序端**静音后能听到对端清晰语音**；**一开麦（对端音频进设备→回声路径激活）就全是杂音**。
⇒ 设备上行健康；**「杂音」仅在双讲（phone 传输激活回声路径）时出现**。

**下行 PCM 量化（`.intercom_dump` 抓 `intercom_downlink.pcm`，48s @16k，15:25 通话）**：
- 全局峰值 **4289（−17.7 dBFS）** → 电平**不低**，「很小」是特定状态崩塌，非链路衰减。
- 两态分明：
  - 干净语音段：rms 600~1000、过零率 **0.07~0.13**（正常说话调制，静音态/设备独讲）。
  - 噪声爆发段（t≈7.5/18.0/19.0/20.5/23.5s）：过零率 **0.39~0.48**（宽带 hiss）、rms 反降到 200~300。
  - 噪声爆发**恰好**出现在 phone 活跃（开麦）时刻。

**根因（高置信）**：后端在双讲时对设备上行做回声处理——线性 AEC 因延迟抖动 `stdev≈1.4 s`
（delay mean≈4.4 s）**锁不准、选错参考帧** → 谱减没消掉回声；而 NER（`×0.3`）只是个**增益**，
消不掉回声、反把近端也砍了。故开麦时：设备语音被砍小（=小）+ 残留回声变宽带 hiss（=杂音）；
静音时无回声路径 → 后端不触发 → 干净通过。

**结论**：这不是 NER 地板问题，是**后端根本消不掉这条回声**。修法 = 把回声消除归属**交回设备侧**
（voicebox 的 line-echo-canceller，用设备播放缓冲做零抖动参考），后端对 8818 改为**透传**
（`server_aec: false`）。这正是 voicebox 之前提的方向，现实测证明**必须**如此。

**风险与下一步**：`server_aec: false` 直接撤掉后端消回声 → 若设备侧 AEC 未完成会**啸叫**，需即时回滚
（config 还原 / `aec_ab.sh restore`）。建议下一步：① arm `.intercom_pair` 抓 mic/ref/out 三元组，
下一通同时录静音态+开麦态，证明「mic 干净 → out 变 hiss」即后端为元凶；② 与 voicebox 确认其
line-echo-canceller 是否已就绪，再决定是否切透传。

—— 后端（新枝）2026-09-10 15:30

### 9.9 决策：不直接切 `server_aec:false`，先切 `2ch_srvref → 2ch`（2026-09-10 15:38 已部署）

**为什么改走 `2ch` 而不是直接透传**：§9.8 结论「后端消不掉回声」是对的，但 `server_aec:false` 有致命风险——一旦设备侧 AEC 没就绪就**啸叫**，且不可逆到要即时回滚。`2ch` 模式是个**零啸叫风险的中间步**：

- `2ch` 模式（`intercom_aec.py:748-764`）：用 `_deinterleave` 把设备 **ch1（播放通道）当回声参考**，`_ref_hist` FIFO + `_select_echo_ref_2ch` 取**上一帧 ch1**，延迟**恒 1 帧 = 20 ms、零抖动**；再用 `_spectral_subtract(mic, ch1)` 做真谱减。
- 对照 `2ch_srvref`：回声参考改走**后端自喂的下行音频**，`delay mean≈4.4 s、stdev≈1.4 s`（`2ch_srvref` diag 全样本），对齐永远选错帧 → 谱减失效。
- **关键安全网**：`2ch` 分支里 NER 兜底**仍在**（`intercom_aec.py:777-792`，`residual_suppress` 默认 True）→ 即便 ch1 仍是坏的、谱减无米下锅，**最坏也只是「和现在一样开麦有杂音」，绝不啸叫**。

⇒ 切 `2ch` 本质是「把回声参考源从『后端抖动的下行』换成『设备自己零抖动的 ch1』」，比透传安全一个量级，且**不撤消回声消除**。

**部署（纯配置，秒级可回滚 `aec_ab.sh restore` 或 config 改回 `2ch_srvref`）**：
- 159 config：`server_aec_mode: 2ch_srvref → 2ch`（intercom 段 4 空格缩进）。
- 重启 → `/metrics 200`；config 复核：`server_aec=True`、`server_aec_devices=['94a9901a8818']`、`aec_residual_suppress=True`、`aec_residual_floor=0.3`、`aec_echo_gain_cap=NONE(→0.8)`、`aec_echo_delay_ms=0`。
- **未 git/docker commit（守先别固）**。

**已 arm 三路抓包（restart 后均存活于 bind-mount `data/tmp/`）**：
- `.intercom_raw2ch`：仅 `2ch` 模式触发，落盘**消回声前的原始 mic + ch1**（铁证：ch1 是否已是合格参考）。
- `.intercom_pair`：mic/ref/out 三元组。
- `.intercom_dump`：手机实际收到的 downlink PCM。

**实测结果（用户 15:53 复拨，关键判决）**：
- 静音态：正常（手机听清设备）。
- **开麦态（双讲）：设备端直接啸叫**。

⇒ **`2ch` 模式对 8818 不安全，已立即回滚 `2ch_srvref`（啸叫停止，回到「不啸叫但小+杂音」）**。

**这把 ch1 的状态钉死了**：之前看到近期 chlvl `ch1_rms=212~814` 误以为 ch1 修好了——但「ch1 有能量」≠「ch1 是播放回声参考」。**实证：拿 ch1 当回声参考做谱减 → 注入不相关能量 → 声学环路增益顶过 1 → 啸叫**。与 09-06 铁证（ch1 中位 RMS=4、60-73Hz 杂散、corr 0.129）一致：**8818 的 ch1 至今不是合格的回声参考**，`2ch` 模式对该设备不可用。

**代码侧旁证**：`intercom_registry.py:727` 确认 `2ch` 模式 `ref_cache` 仍喂（NER 兜底应有），所以啸叫不是 NER 没触发，而是**谱减用了错误参考本身把环路顶爆**——NER ×0.3 压不住错误谱减引入的能量。

**最终结论与路径**：
1. 后端对 8818 **只能走 `2ch_srvref`**（服务端自喂下行 ref），绝不走 `2ch`。
2. `2ch_srvref` 的「小+杂音」根因 = 下行 ref 抖动 4.4s±1.4s 锁不准 → 谱减失效 + NER ×0.3 泵动 → hiss。**后端任何调参都救不了这条抖动**（已试 NER 地板、delay 窗，均不治本）。
3. **唯一真解 = 设备侧 line-echo-canceller**（用设备播放缓冲做零抖动参考，voicebox 此前已提方向）+ 后端 `server_aec: false` 透传。待 voicebox 确认其设备 AEC 就绪后，再切透传（并备即时回滚）。

—— 后端（新枝）2026-09-10 15:58

### 9.10 手机侧啸叫根因：phone 腿 NER 永未启用（2026-09-10 16:16 定位）

**用户回滚 `2ch→2ch_srvref` 后仍啸叫，问「是否小程序端问题」。结论：不是小程序代码问题，是后端 phone 腿 AEC 缺 NER 兜底。**

证据链（全 file:line）：
- phone 腿硬编码 `mode="outband"`（`helloHandle.py:278`）。
- phone 腿 NER 受 `ner_outband` 门控（`intercom_aec.py:423` `elif self.mode=="outband" and self._ner_outband`），而 `ner_outband` 默认 `False`（`:72`）且 helloHandle phone 腿调用（`:278-284`）**从不传该参** → phone NER 永久关闭。
- 本应开启它的 config 键 `aec_phone_residual_suppress`（root 级）= 此前 §8.4 判定的死配置——helloHandle 从不读它 → phone NER 永远不启用。
- phone `ref_cache` 确被喂（`intercom_registry.py:402` `self._feed_intercom_ref(target, pcm)`，target=`phone-*` 时喂设备音频）→ NER 门控可工作，只是没开。
- 设备腿有 NER（本轮 diag `ref_active_rate=0.888`）→ 当前啸叫来自**无 NER 兜底的 phone 腿**。

**机制**：phone 播设备音频 → phone mic 拾回 → 后端 phone 腿谱减用抖动的设备音频参考（4.4s±1.4s，失效，同设备腿）→ **无 NER 兜底** → 回声未消 → phone 声学环路未断 → 啸叫。

**修复（未部署，待授权）**：helloHandle phone 腿 `enable_intercom_aec` 调用补 `ner_outband=bool(_icfg.get("aec_phone_residual_suppress", True))`（默认开，config 可关）。phone 腿即获与设备腿同等的 NER 保护（双讲时 ×0.3），断 phone 环路。代价：双讲时 phone 自身声音对设备略轻（同设备腿已做）。另：step1 设备地板 `0.1→0.3` 已松动设备 NER，可一并回退 `0.1` 增厚安全余量（代价更轻）。

- **部署核验（2026-09-10 16:54，未 commit，守先别固）**：本地 md5 `718d5b9e` == 容器 `/opt/fuguoai-server/fuguoai_core/handle/helloHandle.py` md5；容器活文件 `grep -c ner_outband=_phone_ner = 1`；`docker cp` → 清 `handle/__pycache__` → restart → `/metrics 200`；重启后日志无 import/traceback 报错。待用户复拨验证手机侧啸叫是否消失。

**用户复拨验证（17:09）**：手机侧**啸叫已消失**（phone 腿 NER 生效，断开了手机声学环路）；但**下行「声音很小 + 有杂音」仍在，且「静音也没用」**。
- 「静音也没用」= 手机静音只关自己上行，不影响设备→手机的**下行**；用户听到的就是这条下行链路本身小+杂音 → 证实问题在**下行（设备腿 2ch_srvref AEC）**，与本次 phone 腿 NER 修复无关。
- 下行小+杂音根因（与 §9.8/§9.9 一致）：设备腿 `2ch_srvref` 用抖动 4.4s±1.4s 的下行 ref 做谱减 → 锁不准 → 谱减失效 + NER ×0.3 在双讲时把设备声音压小、残留回声变 hiss。**后端调参（NER 地板 / delay 窗）救不了这条抖动**，此前的「small」在 floor=0.1 原版也一直存在。
- **唯一真解仍是设备侧 line-echo-canceller + 后端 `server_aec:false` 透传**（§9.9 最终路径）。phone 腿 NER 修复只解决了「手机侧啸叫」这一半，下行质量要靠 voicebox 设备侧 AEC。

—— 后端（新枝）2026-09-10 17:10

## §9.11 后端缓解实验：收窄回声搜索窗 + 调大 NER 地板（2026-09-10 17:17 部署，待验证）

- **修正 §9.10(17:10) 的「后端调参救不了」断言**：重读容器活文件 `intercom_aec.py` 的 `process_ref_cache`(321) 后发现，噪声主因不是「抖动本身不可救」，而是**回声参考搜索窗 `[-30, aec_max_delay_ms=12000]` 即 12 秒全窗太宽**——8818 真实回声延迟中位 4.5s±1.5s，12s 窗让语音自相似把 AEC 每帧引导到「声音像但错位的 ref 帧」→ 投影 `g=<mic,ref>/<ref,ref>` 减错真实设备声 + 注入反相噪声 → 「全是杂音」。收窄窗口是真实杠杆，此前误判。
- **日志铁证（17:15 通话 diag）**：device 腿 delay 中位 4.5s、stdev 1.5s、范围 2.1–7.0s，corr 均值 0.43（强相关仅 ~1/3）；`chlvl` 显示设备原始 mic(ch0) RMS 0–116（声在、只是弱）。`_apply_residual_suppress`(567) 确认 `out *= residual_floor`（地板=衰减系数，调大才更响）→ 双讲 ×0.3 致「声音小」。
- **部署两处配置（159 活文件，备份 `/home/ubuntu/.config.yaml.bak-echo-20260910`）**：
  ① `aec_echo_delay_ms: 0 → 4500`（配现成 `aec_echo_delay_band_ms: 2500`）→ 搜索窗收紧 `[2000,7000]`，吃掉 7–12s 错配区。最坏=少数帧透传（干净声+未消回声），绝不变更差。
  ② `aec_residual_floor: 0.3 → 0.5` → 双讲设备声 ×0.5（更响）；环路增益 α×0.5 ≤ 0.35 < 1，不复啸叫。
- **红线**：sed 改→restart→`/metrics 200`(第3次)→无启动报错。未 git/docker commit（守先别固）。
- **待用户复拨验证**：拉 `aec][diag` 看 delay stdev 是否收窄、corr 是否升高、手机端「小+杂音」是否改善。
- **两种结果**：改善 → 后端对 8818 下行有可量化缓解（非根治，仍建议 voicebox 设备侧 AEC 根除）；无效 → 立即回滚，结论回到「唯一真解 = 设备侧 line-echo-canceller + 后端 `server_aec:false` 透传」。
- **回滚命令**：`sudo -n docker exec fuguoai-server sed -i -e 's/^    aec_echo_delay_ms: 4500$/    aec_echo_delay_ms: 0/' -e 's/^    aec_residual_floor: 0.5$/    aec_residual_floor: 0.3/' /opt/fuguoai-server/data/.config.yaml && sudo -n docker restart fuguoai-server`

—— 后端（新枝）2026-09-10 17:30

---

## §10 2026-09-10 18:58 复拨硬实证：设备上行 ch0 在「无下行播放」时中位 RMS=7（−73 dBFS），8 段 ≥1s 掉到数字静音

**复现用户主诉**：「手机端小 + 杂音」+「20 几秒卡了一下，双端无声几秒」。
通话 18:58:41–19:00:04（`phone-Uc7d7cd08de20` ↔ `94a9901a8818`），mode=2ch_srvref。

**抓包口径**：`intercom_raw2ch_srvref.pcm` = 5,245,456 B = **82.0s**，与通话 83s 吻合 → **帧零丢失**，那些静音是**帧内容本身**，不是网络丢帧。且该落盘点在 AEC **之前**（process_opus 入口原始 2ch）→ **非后端所致**。

| 指标 | Phase A：0~44.5s（手机全程闭麦，ch1 恒 0） | Phase B：44.5~82s（开麦，ch1 起跳 394→1541） |
|---|---|---|
| ch0 中位 RMS | **7（−73 dBFS）** | **271（−41.6 dBFS）** |
| ch0 p90 / max | 361 / 5348 | 1874 / 3903 |
| 帧 <10 / <20 / <30 | 57.6% / **68.3%** / 72.1% | 12.2% / 14.4% / 20.3% |
| **≥1s 静默段（RMS<10）** | **8 段，合计 14.3s，最长 3.5s** | **0 段** |
| ≥1s 静默段（RMS<20） | 11 段，合计 21.9s | 0 段 |

**用户所述「20 几秒卡一下」精确定位**：**t = 22.0~24.5s，ch0 = RMS 4（−78 dBFS）连续 3 秒**。其前 40 个 60ms 帧的 RMS 序列为：

```
448 445 541 432 187 78 41 24 12 7 6 5 2 5 4 6 5 3 2 2 2 3 3 3 3 5 4 2 4 3 3 3 3 4 6 4 6 7 3 3
```

语音电平（~440）后快速跌落，**稳定停在 RMS 2~7 并持续 1.5s 以上**。

**判据**：安静房间中的真麦克风底噪不会停在 RMS 2~7（应在 20~60）。**这是采集链路在输出近零，不是「用户说话小声」。**

**最强对照**：同一设备、同一人、同一通电话，**唯一变量是「服务端有没有音频送给它」**——

- 闭麦 ⇒ 服务端无音频可发 ⇒ 设备无播放（ch1 恒 0）⇒ ch0 中位 **7**，8 段成秒掉线；
- 开麦 ⇒ 有下行播放（ch1 起跳）⇒ ch0 中位 **271**，一秒不空。

中位差 **32 dB**，且掉线段全部消失。指向**设备端采集链路与播放状态耦合 / 采集任务饥饿**（与此前已定位的 `AudioLoop` 忙循环饿死 uplink 属同族）。

**后端侧说明（不构成主因）**：运行配置 `aec_residual_suppress=true` + `aec_residual_floor=0.3`，双讲时在设备上行之上再减 10.5 dB，属**次要放大项**；主因在设备上行本身缺内容。

**顺带更正 §9.11 的延迟结论**：AEC diag 报的 `delay 中位 4.5s / stdev 1.5s / 范围 2.1–7.0s` **是假峰**——实测 **ch1（设备播放通道）与 ref 的包络延迟仅 140ms**，且 **ch1↔ch0 样本级最大 |r|≈0.077**（逐 5s 仅 0.10~0.15，与 09-06 记录的 corr 0.129 一致）。故 §9.11「错位 ref 帧导致杂音」的叙事不成立。17:17 部署的实验配置（`aec_echo_delay_ms: 4500` / `aec_residual_floor: 0.5`）**已回滚**，线上现值 `echo_delay_ms=0`、`residual_floor=0.3`、`multi_tap=false`。

**通道定性（本轮闭合，供固件侧对齐）**：

- **ch0 = 设备麦克风**。铁证：闭麦段（无播放）用户对设备说话时，ch0 逐 5s 达 286/366/443（peak 最高 1842）而 **ch1 100% 逐样本为 0**；开麦瞬间 ch1 才起跳。
- **ch1 = 设备播放通道**（随播放信号走）。闭麦段恒 0、开麦段与下行同步。**注意：它不是干净的线性回声参考**（样本级 |r|≈0.077），不能用于线性 AEC，故后端维持 `2ch_srvref` 自喂参考是正确选择。

**音频证据**（16k mono wav，原始电平未加增益，可直接听）：

- 闭麦段全段 44s（`E_闭麦段全段44s_设备mic.wav`）
- 掉线窗口 15–28s（`F_掉线窗口15-28s_设备mic.wav`）
- 开麦段 45–82s 对照（`G_开麦段45-82s_设备mic.wav`）

**给 voicebox 的明确请求**：请确认并修复——在**无下行播放**的时段，设备 2ch 上行 ch0 为何输出数字零。建议核对：① mic 采集任务是否被饿死/优先级；② I2S 读是否在无播放时停摆；③ esp-sr AFE/NS 的门限是否在低信噪比下直接硬零输出。

—— 后端（新枝）2026-09-10 19:15

---

## §11 定案更正：上行近零 = 设备端上行啸叫抑制器（作废 §10 的三点猜测）

> ⚠️ **本节结论已降级，请看 §12（2026-09-10 19:5x 修订）**：HowlFft 只是「幅度/时长吻合」的候选①，且与随附 bin 中已含的 `[HOWL-FFT] GUARD reset` 存在矛盾；§12 给出更严的证据分级与三步定案实验。本节保留作历史。

> 2026-09-10　依据：设备侧固件源码 `cnb.cool/fuguoai/fuguoai-ble-intercom` @ `887ed7e`
> 完整报告：`对讲_设备上行近零_固件侧根因_20260910.md`

**§10 请求核查的三点（mic 任务饿死 / I2S 无播放停摆 / esp-sr AFE 硬零）——均不成立，作废。** 拿到源码后改判如下。

**1. 数据（同一份 82s 抓包，抓包点在服务端处理之前）**

| 指标 | 无下行段 0–49s（ch1≡0） | 有下行段 49–82s |
|---|---|---|
| ch0 近零帧（RMS<25）占比 | **72.3%** | 16.8% |
| ch0 中位 RMS | **6.8** | 193.8 |
| 最长连续近零 | **3.62 s** | 0.68 s |

有声明段中位 RMS 272.4 vs 近零段 4.9 ⇒ **整带 −34.9 dB**。近零段最小 1.4–4.2，低于真麦克风底噪（20–60）。

**2. 排除项**

- **后端 NER 不是主因**：只 −10.5dB，且被 `ref_active` 门控，而 79% 近零段设备侧无下行 ⇒ 不生效；抓包点在其之前。
- **设备端 `[HOWL-GUARD]` AGC 也不是**：门控 `far_end_playing = ch1RMS > 阈值`，近零段 ch1 恒 0 ⇒ 不动作；且它压的是下行（喇叭），方向反了。

**3. 根因（源码锚点）**

上行链路：`IntercomAecTaskLoop` → esp_aec → 100Hz HPF → **`RunHowlSuppressorUplink()`（`main/application.cc:2330`，调用点 `:2645`）** → `InterleaveMicWithRef` → opus。

`HowlFftProcess()`（`:2185`）：512 点 FFT/WOLA，hop 256＝**16ms/帧**；窄峰 → bin 增益拉到 `kHfSuppress=0.01`（**−40dB**，`:2084`）。

- **幅度闭合**：−40dB ↔ 实测 −34.9dB。
- **时长闭合**：旧 `kHfHoldFrames=187` × 16ms = **2.99s** ↔ 实测 3.62/3.30/2.82/2.46s。
- 源码注释自陈同症（`:2087`、`:2248-2252`）：「3s 锁存把人声一起碾掉」「±6bin 膨胀写回 persist → 约 35 帧吞掉全部 217 bin，**上行全带静音 → 手机端听不见/声音极小**」。即：**不是新问题**。

**4. 一个关键推论：复测那版不是 `887ed7e`**

`887ed7e` 已含今天全部修复，三闸都会拦住「3s 全带 −40dB」：`kHfHoldFrames 187→8`（≤0.13s）、`kHfSuppressBudget=32`（最多压 15% bin）、`[HF-GUARD] >150bin 持续 16 帧即复位`。实测坐实 6 段 ≥2.4s 整带近零 ⇒ 设备跑的是**修复前**构建。

**5. 请求（二选一，请回执）**

- **首选**：刷入 `887ed7e`／`e9549423` 构建的固件复拨，按下方判据回填结果；
- 若暂不方便刷：复拨时**抓一份设备串口**，取 `[HOWL-FFT] ENGAGE bins=N` / `release` / `GUARD reset` 三种行即可判案（`bins` 爬向 ~200 = 全带级联实锤）。

**验收判据（硬指标，抓包法不变：`data/tmp/` 放 `.intercom_raw2ch` + `.intercom_refdump`，拨一通取 `intercom_raw2ch_srvref.pcm`）**

| # | 判据 | 通过线 | 本轮实测 |
|---|---|---|---|
| V1 | 无下行段 ch0 近零占比 | <5% | **72.3%** FAIL |
| V2 | 最长连续近零 | <0.5s | **3.62s** FAIL |
| V3 | 有声明/近零段中位 RMS 差 | <15dB | **34.9dB** FAIL |
| V4 | 主观听感（需上机） | 手机端复声、无成秒断续 | — |

若 V1–V3 仍 FAIL，则压制源不止 HowlFft，下一步嫌疑为本地 `esp_aec` 的 NLP 路径（`aec_process`），届时再查。

**6. 后端侧动作：不动。** 本轮不改配置、不加抑制补丁（NER 非主因）；临时诊断补丁仍守"不 commit"，等复拨出证据后清理。

—— 后端（新枝）2026-09-10 20:05

---

## §12 §11 结论降级 + 快照文件事实（2026-09-10 19:5x 修订）

> ⚠️ **本节 12.1 里"设备跑的是修复前构建"的前提已作废，请看 §13**（设备实际跑 `e9549423`＝降锁存版）。本节其余事实（快照文件构成、bin 描述符）仍成立，保留作历史。

> 完整报告：`对讲_设备上行近零_固件侧根因_20260910.md`（v2 修订版）

### 12.1 §11 说过了的三处，更正

| §11 说法 | 核实结果 |
|---|---|
| 「根因定案 = HowlFft，请刷 `887ed7e` 复拨」 | **降级为候选①**。只有幅度/时长吻合；且随附 bin 内**已含** `[HOWL-FFT] GUARD reset`（抑制 bin >150 连续 16 帧即复位 ⇒ 全带压制最多 ≈0.26 s），与实测 **3.62 s 连续近零矛盾**。若设备跑的就是该 bin，候选① 不成立 |
| 「`887ed7e` = 完整可编译的设备固件源码」 | 该 commit **无 `components/` 目录**（`.gitignore` 明确要保留 6 个 `fuguo-*` 自建组件，实际都没进）；AEC 依赖第三方 `esp_aec.h`（`managed_components/`，按设计不入库）⇒ **快照不完整、不能独立复现构建** |
| 「`887ed7e` 与 `e9549423` 构建态一致 ⇒ 已含修复」 | 同 commit 内确有 `fuguoai-ble.bin`，sha256 前缀正是 `e9549423`，但其 app 描述符为 **`version=a5b70c9-dirty`、`Compile time 17:38:31 Sep 2 2026`、IDF v5.5.4** ⇒ 它是 **9/2 的旧构建**，与含 9/10 修复的源码**不是一版** |

### 12.2 未变的部分（实测，可直接采信）

抓包点在服务端处理之前，ch0 = 设备原始上行：

| 指标 | A 段 0.86–45.06 s（设备无播放，ch1 中位 0.00 / p95 0） | B 段 45.1–82.0 s（有播放） |
|---|---|---|
| ch0 近零帧（RMS<25）占比 | **72.9%** | 21.9% |
| ch0 中位 RMS | **6.8** | 145.3 |
| 最长连续近零 | **3.62 s** | 0.88 s |

- 有声明段中位 272.4 vs 近零段 4.9 → **整带 −34.9 dB**；近零段最小 1.4–4.2（**低于真 mic 底噪 20–60**）。
- 28 个 ≥0.3 s 近零段中 21 段（75%，合计 27.4 s）落在**设备无播放**窗口 ⇒ 与下行无关。
- 硬排除：后端 NER（上限 −10.5 dB + `ref_active` 门控 + 抓包点在其之前）；设备端 `[HOWL-GUARD]` AGC（门控 `far_end_playing`，且压的是**下行**）。

**唯一确证的结论：设备在 opus 编码前，把上行成秒级、整带压低约 35 dB。**

### 12.3 候选压制级

① `HowlFftState` 上行抑制器（`application.cc:2330/2645`）——幅度 −40 dB 档、187×16 ms≈3 s 最吻合；**但与 bin 内 GUARD 矛盾**（见 12.1）。
② 官方 esp-sr `esp_aec` 的 NLP（`aec_nlp_process`）——链路上唯一另一级能整带压制；第三方、快照无源码无法静态核对；**且官方 esp_aec 在本项目有"NLP 确定性卡死（2026-08-24 实测）"前科**（本机线因此改用自研 NLMS）⇒ 高危件。
③ 100 Hz HPF —— 不可能 −35 dB，排除。 ④ AFE 的 VAD 门控 —— 代码上 `output_callback_` 无条件回调，排除。

### 12.4 请设备侧配合的三步（按成本排序）

1. **E1（0 成本）**：读设备启动日志 `App version` / `Compile time`，确认跑的到底是哪一版（与 `a5b70c9-dirty` / `Sep 2 2026 17:38:31` 比对）。版本不一致则一切按实际版本重判。
2. **E2（一次通话）**：抓设备串口，只看三类行——`[HOWL-FFT] ENGAGE/active bins=N`（爬到 ~200 = ① 实锤；稳定 <32 = 与 ① 无关）、`[HOWL-FFT] GUARD reset`（近零段内出现 = 压制源另有其人）、`[AEC-ERLE] … ratio=`（大 = ② 在猛压）。
3. **E3（抓不到串口时的二分）**：分别出「HowlFft 旁路」与「本地 esp_aec 旁路」两版，各拨一通按同口径抓包，哪版近零段消失即压实。

判据不变：V1 无下行段近零占比 <5%（现 72.9% FAIL）／V2 最长连续 <0.5 s（现 3.62 s FAIL）／V3 两段中位差 <15 dB（现 34.9 dB FAIL）／V4 主观复声（需上机）。

**后端侧：依旧不动**（NER 非主因）；临时诊断补丁 md5 `4cb83b90…`，未 commit。

—— 后端（新枝）2026-09-10 19:5x

---

## §13 更正 v3：设备版本前提修正 ⇒ 候选① 基本排除，本地 esp_aec 成首选

> 依据：**板子实际跑的固件 = 二进制 SHA `e9549423…`（降锁存版，`kHfHoldFrames 187→8`，内部 v2.0.1 / build3）**；`887ed7e` 只是 intercom 仓库的 git 提交号。§11/§12 里"设备跑修复前构建、须刷 887ed7e"的前提作废。
> 完整报告：`对讲_设备上行近零_固件侧根因_20260910.md`（v3）

### 13.1 新增关键统计：压制**条件于"没有 ref 可吃"**

按 20 ms 帧统计（ref 活跃 = 当前帧 ±150 ms 内设备播放参考 RMS>50）：

| 条件 | 帧占比 | ch0 近零率 | ch0 中位 RMS |
|---|---|---|---|
| ref 静默窗（设备无播放） | 56% | **73.2%** | **6.8** |
| ref 活跃窗（设备在播放） | 44% | **20.2%** | 158.5 |

28 个 ≥0.3 s 近零段中 **21 段（75%，合计 27.4 s）完全无 ref 可用**。

**方向性判据**：链路里唯一"吃 ref"的是**本地 AEC**。NER 是反向门控（ref 活跃才压）⇒ 与观测相反；HowlFft 不消费 ref、也不按 ref 门控 ⇒ 解释不了这条相关性。

### 13.2 候选① HowlFft：**基本排除**

1. 设备跑的就是降锁存版（hold 187→8 ≈0.13 s），**没有 3 s 锁存**；
2. `e9549423` 的 bin 内含字串 `[HOWL-FFT] GUARD reset (bins=%d, fires=%d)` ⇒ 抑制 bin>150 且连续 16 帧（≈0.26 s）即复位 ⇒ **产不出 3.62 s 连续全带近零**（应表现为 ≤0.8 s 的周期性"压—复位"，中间必有放行帧，实测没有）。

### 13.3 候选② 本地 esp_aec（官方 esp-sr）：**首选，但未实证**

- 源码可疑点：`[AEC-REF-NEVER-ZERO]` 的兜底是「参考活动后保持最后活跃样本 **320 ms**（`application.h:485 kAecRefActiveUs=320000`），超时即回落到喂 **0**」（`application.cc:2436-2441`）。
  本通 A 段无下行 44 s ⇒ 其中约 **43.7 s** 本地 AEC 吃的是**全零参考**，正是近零率 73.2% 的那段。
- ⚠️ 未实证：esp_aec 属第三方（`managed_components/`，快照无源码），"全零参考下压死输出"无法静态核对；且该项目对此件有前科（官方 esp_aec 的 NLP 在 90C0 PSRAM 配置下确定性卡死 → 10s task_wdt，2026-08-24 实测）。

### 13.4 定案实验（已收窄，二选一）

- **E2 抓设备串口**（一次通话）：看 `[AEC-ERLE] ein=… eout=… eref=… ratio=…` —— 近零段内 `ratio` 很大且 `eref`≈0 ⇒ **本地 AEC 实锤**；同时看 `[HOWL-FFT] active bins=N`（稳定 <32）与有无 `GUARD reset` 双重确认排除 ①。
- **E3 二分（最便宜）**：出「ref 不活跃时本地 AEC 直通（`out = mic`）」一版，同口径抓包：近零占比 72.9%→<5% ⇒ 定案，且**修复本身就是这一行旁路**（只影响无下行时段，风险极小）；仍 72.9% ⇒ 压制在 AFE/mic 侧，再往前查。

判据不变：V1 <5%（现 72.9% FAIL）／V2 <0.5 s（现 3.62 s FAIL）／V3 <15 dB（现 34.9 dB FAIL）／V4 主观复声（需上机）。

**后端侧：依旧不动**；临时诊断补丁 md5 `4cb83b90…`，未 commit。

—— 后端（新枝）2026-09-10 19:40
