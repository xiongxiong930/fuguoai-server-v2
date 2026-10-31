# 日程同步 id 命名空间修订 + once 清理闭环（2026-10-18）

> 状态：**已部署 159，未 commit**（守"先别固"：等用户实测验证后随版本进位固化）。
> 本文为专题设计/复盘记录（v1），对应 CHANGELOG「未发版」段。

## 1. 背景与用户实测现象

2026-10-18 用户实地观察 8818 设备日程屏，四个现象：

| 现象 | 定性 |
|---|---|
| 屏上只显示 2 条（以前是 4 条） | **id 钳位覆盖**：设备表只有一个云端任务槽位 |
| 本地设的任务显示"云通" | 非故障：`ScheduleRowPrefix` 的 `[来源·分类]` 格式（source=cloud → "云"，category=0 → "通"） |
| 设完立即看屏没有 | 对话态遮罩：`HideSchedule` 把日程面板换成气泡区 + `RefreshSchedule` 5s 节流 |
| 过时间了也没清除、成了长期任务 | once 触发后镜像不清理 + 历史字符串 repeat 被固件 fallback 成 0x7F=每天 |

## 2. 根因：cJSON `valueint` 溢出钳位

固件 `JsonToTask`（device_control.cc）用 cJSON 读 `it->valueint`（C `int`）。
cJSON 对 >INT_MAX 的数把 `valueint` **饱和钳位成 2147483647**。

旧约定（代码注释所称"契约 §2.3/§4.2/§5.2"）：云端 id 从 `0x80000000`（2147483648）
起递增，靠高位 bit 与本地 id 隔离。⇒ **所有 >=0x80000000 的云端 id 到设备侧
全部变成 2147483647**：

- 设备表里云端任务只有一个槽位，后到者按 LWW（`modified_utc` 新者胜）覆盖先到者；
- 8818 实证覆盖三次：早餐(8:00, mod 1792289345) → 喝水10:30(mod 1792289545) →
  喝水10:50(mod 1792291693)，设备永远只剩 1 条云端任务；
- deletes 按钳位 id 也可能误删。

## 3. 新约定：云端 id 从 INT_MAX 递减分配

| 项 | 值 | 依据 |
|---|---|---|
| `CLOUD_ID_MAX` | 0x7FFFFFFF（2147483647） | cJSON valueint 不钳位上限 |
| `CLOUD_ID_FLOOR` | 1_000_000_000 | 递减下限，与本地 id 空间留 1e9 余量；耗尽抛 `ValueError` |
| `CLOUD_ID_HIGH_BIT` | 0x80000000 | **降级**：仅识别迁移前 legacy 存量高位 id |

分配算法（`create_cloud_task`）：`new_id = min([CLOUD_ID_MAX + 1] + 现有云端 id) - 1`。

与本地 id 不碰撞的证明：固件 `AddTask`（scheduler.h:144-149）对**无高位 bit** 的 id
会推进 `next_id_`。首个 2147483647 落位后 `next_id_=2147483648`，之后本地 id 从
2147483648 往上分配、云端 id 从 2147483647 往下分配，两个方向**永不相交**。

命名空间判定（`_is_cloud_task`）：**只认显式 `source` 字段**（"cloud"/1/"1"），
不再依赖 id 区间——新分配 id 没有高位 bit，区间判定会漏判。legacy 高位 id 仅作
无 source 存量兜底。

## 4. once 清理闭环

链路：`networked_reminder_create` → `_mirror_to_device_schedule(..., rid=rid)`
（rid 写入镜像条目）→ 到点 `fire_one` 播报成功 → `_cleanup_fired_mirror`：

1. `store.get(device_id, rid)` 取提醒，判 `schedule.type == "once"`（daily/weekly 不清）；
2. `delete_cloud_task_by_rid(device_id, rid)` 删镜像，返回被删 id；
3. `push_schedule_to_device(device_id, tasks=[], deletes=[id])` 实时下行删除；
4. 全程 best-effort：异常只记 warning，不影响 `fire_one` 主流程与播放结果。

cancel 路径（`_delete_mirror_task`）：rid 精确删除优先；内容匹配
（hour/minute/repeat/date_y/m/d/message）仅作 legacy 无 rid 存量兜底——同内容
多条提醒按 rid 取消不会误删另一条。

设备侧兜底：固件 `ExpireOnceTasks`（scheduler.h:185-209）只清昨天及更早的 once，
当天的过点 once 保留给 `CheckDue` 12h 补播窗口——所以后端主动清理是必需的。

## 5. pull 回灌 stale deletes（新机制）

旧 pull 永远 `deletes=[]`：设备没收到实时 delete 时（离线/掉线窗口），fired/
cancel 的镜像条目会残留。

新机制：
- `iot_handler` M1 分支：`merge_full_sync` 之后（锁外）`save_device_table(device_id, schedule)`
  原样留存设备上报全表 → `data/schedule_device_tables.json`；
- `get_stale_cloud_deletes(device_id)`：设备表 cloud ids − mirror cloud ids（内部直接
  读 db，避免与非递归 `_lock` 死锁）；
- `SchedulePullMessageHandler`：pull 时 tasks=云端全量 + deletes=stale 差集。

覆盖场景：once fired 后设备离线未知情、cancel 后掉线、id 重映射迁移前的 legacy 残留。

⚠️ 语义边界：设备上报的 cloud 项**不进 mirror**（后端是云端真源），因此设备表里
有、mirror 没有的云端 id 一律视为孤儿待清。mirror 数据丢失的场景下这会清掉设备
上的云端任务——可接受（云端真源丢失即任务不存在）。

## 6. 存量迁移（tools/migrate_schedule_mirror_ids_20261018.py）

两段式（--dry-run 默认，--apply 落盘）：

1. 备份 `data/schedule_mirror.json` → `schedule_mirror.json.bak-<ts>`；
2. 对每个设备的 cloud 条目按 `modified_utc` 降序重映射：从 `INT_MAX-100` 起递减
   （**不用 INT_MAX 本身**：设备侧被钳位的 legacy id 恰是 2147483647，留一档
   头部余量防"同帧 delete+add 同 id"的时序歧义）；
3. bump `modified_utc = now`（固件 LWW 严格递增才更新，相等且双方 cloud 不更新）；
4. 删 `type=once` 且目标日期 < 今天的条目（设备必隐藏，无镜像价值）；
5. 补 `source: "cloud"` 字段。

迁移后设备下次重连 pull：deletes=旧 legacy id（设备侧 2147483647）+ tasks=新 id
全集，固件 `HandleSchedulePush` 先删后加完成自愈。

## 8. 屏显 [云·通] 问题：后端无法修复，需固件决策（2026-10-18 追加复盘）

用户两次反馈语音设的提醒在设备屏显示 `[云·通]`，期待与设备本地任务一致。

**显示链路（固件回源）**：`ScheduleRowPrefix`（display/schedule_view.h:46-56）按
`source != 0 → "云"`、category 0 → "通" 生成前缀。设备侧 source 来自
`JsonToTask`（device_control.cc:1668-1687）：字符串 "cloud"/数字非 0/高位 id
兜底 → kCloud。

**后端尝试与回退**：曾让下行态剥离 source 字段（`_to_device_task` 不再输出
source），期望设备解析为 kLocal 显示 [本·通]。**实测不可行且有害，已回退**：
1. 固件对 schedule_push 到达的任务统一标记 kCloud——8818 实证：无线 source
   的载荷，设备 M1 上报仍为 source=1，剥离达不到显示目的；
2. **与删除路径根本冲突**：`HandleSchedulePush` 的 deletes 只删
   `source==kCloud || id 高位` 的任务（device_control.cc:1626）。新 id
   （INT_MAX 递减）无高位 bit，一旦设备存成 kLocal，fired once 的下行删除
   会被静默跳过 ⇒ §4 的清理闭环失效、"过时间了也没清除"复发。

**结论**：屏显 [云] 由固件单方决定，后端无法通过协议字段改变（一个 source
字段同时被屏显和 deletes 使用，语义冲突）。要改只能固件侧，选项（**要不要做
由用户定，后端不擅自改固件**）：
- A. `ScheduleRowPrefix` 去掉云/本区分，统一显示 `[通]`（终端用户无感知成本）；
- B. 统一显示 `[本·通]`（伪装成本地任务，与设备语音路径设的任务一致）；
- C. 维持现状（[云] 对联调期识别任务来源有信息量）。

注意：8818 为跨团队设备，固件改动涉及烧录与对方团队协同；改后需重刷 + 实测。

## 9. 验证基线

- 单测：`test_schedule_mirror_store.py` 24/24、`test_schedule_downlink.py` 19/19、
  `test_networked_reminder_mirror.py` 19/19、`test_networked_reminder_fired_cleanup.py` 5/5、
  `test_networked_reminder_store.py` 17/17、`test_networked_reminder_breaker.py` 8/8
- 部署验证（159）：md5 零差异 + 活文件 grep + /metrics 200 + 启动 0 ERROR
- 实测验收（待用户）：设备重连后 M1 → 云端任务恢复多条共存；once 触发后当日清理；
  屏上不再出现"4 条变 2 条"

## 10. 关联

- B 方案主体（语音提醒→设备日程镜像）：`_mirror_to_device_schedule`
- 下行归一化（字符串 repeat → 设备位图）：`schedule_downlink._to_device_task`
- 固件侧回源锚点：`device_control.cc`（JsonToTask/GetUpcomingSchedule/HandleSchedulePush）、
  `scheduler.h`（AddTask/ExpireOnceTasks/CheckDue）
