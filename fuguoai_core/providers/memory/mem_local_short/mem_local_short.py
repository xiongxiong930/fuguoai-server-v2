"""短期记忆提供方（复果自有实现）。

职责：把一轮对话总结成结构化记忆，落到按角色隔离的本地 YAML 文件，
并在后续轮次按查询词做轻量词法检索，把命中的条目注入上下文。

复果自有演进点（逐字保留，不得随意改动）：

* **按角色分桶的状态**：``MemoryProvider`` 是进程级单例，被所有连接共享。
  可变状态（记忆文本 / 结构化索引 / 该角色的 LLM 实例）一律按 ``role_id``
  分桶存放，杜绝并发连接互踩导致的跨设备串写。
* **状态淘汰**：分桶只增不删会泄漏 ⇒ TTL 淘汰 + 硬上限按最久未用驱逐。
* **原子写 + 跨进程锁**：先写临时文件再 ``os.replace``，避免读到半截内容；
  额外用独立 ``.lock`` 文件 + ``flock`` 串行化多实例共享数据卷时的读-改-写。
* **损坏自愈**：记忆文件解析失败时先备份留存（不静默整文件清零），
  本轮只更新当前角色。
* **索引合并而非整体替换**：每轮总结只覆盖它提到的条目，旧事实保留，
  重复提及加权，容量超限按 (weight, ts) 驱逐最轻最旧 —— 修「重建即遗忘」。
* **总结失败即降级**：记忆是增强项不是对话必需品，失败保留上一轮记忆，
  绝不用用户可感的卡顿去换一次记忆更新。
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import time

import yaml

from ..base import MemoryProviderBase, logger
from fuguoai_config.config_loader import get_project_dir
from fuguoai_config.manage_api_client import generate_and_save_chat_summary
from fuguoai_core.utils.util import check_model_key

try:
    from fuguoai_core.utils.metrics import METRICS
except Exception:
    METRICS = None


TAG = __name__

# 测试角色 device-id 前缀：回归/e2e 测试使用的设备以此开头。
# 这些角色的记忆读写被路由到专用测试文件(.memory.test.yaml)，不污染生产记忆库。
# 注意：device_id 在连接入口已被归一化（小写 + 去除冒号/连字符），故 "test-conc-0"
# 会变为 "testconc0"——前缀判定必须同时兼容归一化后的 "test"/"smoke" 形态，否则
# 测试设备会被误判为生产角色、记忆写进 .memory.yaml 造成污染。
_TEST_ROLE_PREFIXES = (
    "test", "smoke",
    "test-", "test_", "smoke-", "smoke_",
    "TEST", "TEST_", "SMOKE", "SMOKE_",
)

# 记忆块裁剪上限：防止无界膨胀导致 LLM 输出被 max_tokens 截断而整轮记忆丢失。
# 记忆立方仅保留最近若干条（旧事件权重低，且身份/偏好等硬事实已落结构化索引长期存储，
# 不因此丢失）；若裁剪后仍超字符上限，继续丢弃最旧记忆立方直至达标。
_TRIM_MAX_CUBE = 18
_TRIM_MAX_CHARS = 2600

# 总结 LLM 的生成预算：最终记忆必被 _trim_memory 裁到 2600 字符(~2k token)，
# 更大预算只会让总结写更久（40~50s），在同 key 上与主对话撞车。
_SUMMARY_MAX_TOKENS = 2048
_SUMMARY_TEMPERATURE = 0.2


short_term_memory_prompt = """
# 时空记忆编织者

## 核心使命
构建可生长的动态记忆网络，在有限空间内保留关键信息的同时，智能维护信息演变轨迹
根据对话记录，总结user的重要信息，以便在未来的对话中提供更个性化的服务

## 记忆法则
### 1. 三维度记忆评估（每次更新必执行）
| 维度       | 评估标准                  | 权重分 |
|------------|---------------------------|--------|
| 时效性     | 信息新鲜度（按对话轮次） | 40%    |
| 情感强度   | 含💖标记/重复提及次数     | 35%    |
| 关联密度   | 与其他信息的连接数量      | 25%    |

### 2. 动态更新机制
**名字变更处理示例：**
原始记忆："曾用名": ["张三"], "现用名": "张三丰"
触发条件：当检测到「我叫X」「称呼我Y」等命名信号时
操作流程：
1. 将旧名移入"曾用名"列表
2. 记录命名时间轴："2024-02-15 14:32:启用张三丰"
3. 在记忆立方追加：「从张三到张三丰的身份蜕变」

### 3. 空间优化策略
- **信息压缩术**：用符号体系提升密度
  - ✅"张三丰[北/软工/🐱]"
  - ❌"北京软件工程师，养猫"
- **淘汰预警**：当总字数≥900时触发
  1. 删除权重分<60且3轮未提及的信息
  2. 合并相似条目（保留时间戳最近的）

## 记忆结构
输出格式必须为可解析的json字符串，不需要解释、注释和说明，保存记忆时仅从对话提取信息，不要混入示例内容
```json
{
  "时空档案": {
    "身份图谱": {
      "现用名": "",
      "特征标记": [] 
    },
    "记忆立方": [
      {
        "事件": "入职新公司",
        "时间戳": "2024-03-20",
        "情感值": 0.9,
        "关联项": ["下午茶"],
        "保鲜期": 30 
      }
    ]
  },
  "关系网络": {
    "高频话题": {"职场": 12},
    "暗线联系": [""]
  },
  "待响应": {
    "紧急事项": ["需立即处理的任务"], 
    "潜在关怀": ["可主动提供的帮助"]
  },
  "高光语录": [
    "最打动人心的瞬间，强烈的情感表达，user的原话"
  ]
}
```
"""


def _repair_json_string(s: str) -> str:
    """对 LLM 常见 JSON 瑕疵做低损修复，提升严格 json.loads 通过率。

    仅做格式层面的无损/低损修正（尾随逗号、单引号、BOM、行注释），
    不改变语义。返回修复后的字符串；无法处理时返回原串（交由调用方判错）。
    """
    t = s.lstrip("\ufeff")
    # 去除 // 行注释（LLM 偶尔夹带）
    t = re.sub(r"//[^\n\r]*", "", t)
    # 尾随逗号：,] 或 } 之前多余逗号
    t = re.sub(r",\s*([\]}])", r"\1", t)
    # 单引号 -> 双引号（键与值），启发式替换
    t = re.sub(r"(?<=[\{\[,:\s])\s*'([^']*?)'\s*:", r'"\1":', t)
    t = re.sub(r":\s*'([^']*?)'\s*([,\}\]])", r':"\1"\2', t)
    return t.strip()


def extract_json_data(json_code):
    """从 LLM 返回文本中稳健抽取 JSON 字符串。

    兼容多种输出形态：```json / ``` 代码围栏、前后夹带说明文字、以及
    直接就是 JSON。抽取失败返回 ""（由调用方优雅降级，不抛异常）。
    增强（记忆总结鲁棒性）：围栏/子串分支做格式修复 + 严格校验，
    避免"已抽取但非法"被上层整轮丢弃；调用方可安全假设非空返回值即合法 JSON。
    """
    if not json_code or not isinstance(json_code, str):
        return ""
    s = json_code.strip()

    def _valid(text):
        try:
            json.loads(text)
            return True
        except Exception:
            return False

    def _try(text):
        # 直接 / 修复后两种都试，返回第一个通过严格解析的串
        for cand in (text, _repair_json_string(text)):
            if cand and _valid(cand):
                return cand
        return ""

    # 1) 代码围栏：优先 ```json，其次任意 ``` 围栏
    fence = None
    for f in ("```json", "```JSON", "```"):
        if s.find(f) != -1:
            fence = f
            break
    if fence:
        start = s.find(fence) + len(fence)
        end = s.find("```", start)
        body = s[start:end].strip() if end != -1 else s[start:].strip()
        out = _try(body)
        if out:
            return out
        # 围栏内仍失败：body 可能夹说明文字，落入步骤 3 抠子串兜底
    # 2) 无围栏：先尝试整体解析
    out = _try(s)
    if out:
        return out
    # 3) 从正文抠第一个 { / [ 到最后一个 } / ]
    candidates = [i for i in (s.find("{"), s.find("[")) if i != -1]
    ends = [i for i in (s.rfind("}"), s.rfind("]")) if i != -1]
    if candidates and ends:
        first, last = min(candidates), max(ends)
        if last > first:
            sub = s[first : last + 1]
            out = _try(sub)
            if out:
                return out
    logger.bind(tag=TAG).warning(f"extract_json_data 无法解析记忆JSON: {s[:120]!r}")
    return ""


def _trim_memory(text, max_cube=_TRIM_MAX_CUBE, max_chars=_TRIM_MAX_CHARS):
    """记忆块裁剪：防止无界膨胀导致 LLM 输出被 max_tokens 截断而整轮记忆丢失。

    - 记忆立方仅保留最近 max_cube 条（旧事件权重低，且身份/偏好等硬事实已落结构化索引长期存储，不因此丢失）
    - 若裁剪后仍超 max_chars，继续丢弃最旧记忆立方直至达标
    - 身份图谱 / 关系网络 / 待响应 / 高光语录 全部保留
    解析失败则原样返回（交由上层降级）。
    """
    try:
        data = json.loads(text)
    except Exception:
        return text
    if not isinstance(data, dict):
        return text
    arch = data.get("时空档案")
    if isinstance(arch, dict):
        cube = arch.get("记忆立方") or []
        if isinstance(cube, list) and len(cube) > max_cube:
            arch["记忆立方"] = cube[-max_cube:]
    cube2 = (data.get("时空档案") or {}).get("记忆立方") or []
    out = json.dumps(data, ensure_ascii=False)
    while len(out) > max_chars and len(cube2) > 4:
        cube2.pop(0)
        out = json.dumps(data, ensure_ascii=False)
    return out


def read_role_memory(role_id: str):
    """脱离活跃连接，直接从磁盘读取某角色的短期记忆文本与文件最后修改时间。

    只读：不修改任何内存状态、不触发保存、不影响连接初始化。供 HTTP 接口
    （小程序设备管理的「互动记忆摘要」）按 device_id(=role_id) 安全读取。
    返回 (memory_text: str, mtime: int|None)。文件不存在/异常返回 ("", None)。
    """
    try:
        rid = str(role_id or "")
        base = get_project_dir() + "data/"
        is_test = rid.startswith(_TEST_ROLE_PREFIXES)
        mpath = base + (".memory.test.yaml" if is_test else ".memory.yaml")
        if not os.path.exists(mpath):
            return "", None
        mtime = int(os.path.getmtime(mpath))
        with open(mpath, "r", encoding="utf-8", errors="replace") as f:
            all_memory = yaml.safe_load(f) or {}
        if not isinstance(all_memory, dict):
            return "", mtime
        return all_memory.get(rid, "") or "", mtime
    except Exception as e:
        logger.bind(tag=TAG).warning(f"读取角色记忆失败 role={role_id}: {e}")
        return "", None


class MemoryProvider(MemoryProviderBase):
    """本地短期记忆提供方：按角色隔离的 YAML 记忆库 + 结构化索引 + 轻量检索。"""

    # 角色状态淘汰：防止多设备长期运行内存只增不减（进程单例 _state 只增不删会泄漏）
    _STATE_TTL = 3600.0   # 1 小时未活动的角色状态淘汰
    _MAX_ROLES = 512      # 硬上限，超出按 last_used 驱逐最旧

    # 索引容量上限（identity/preference 常驻豁免）
    _MAX_IDX_ENTRIES = 200

    def __init__(self, config, summary_memory):
        super().__init__(config)
        # 每角色可变状态隔离：role_id -> {"mem": 短期记忆文本, "idx": 结构化索引}
        # MemoryProvider 为进程级单例被所有连接共享，但记忆状态按角色分桶，
        # 杜绝并发连接互踩导致的跨设备串写（曾致测试设备加载到他人历史记忆）。
        self._state = {}
        self._current_role_id = None  # 记录当前角色，配合 _state[role_id] 消除单例 self.llm 共享
        self.save_to_file = True
        # 写锁：避免多会话并发保存时读-改-写同一文件产生竞态损坏（曾致 .memory.yaml 损坏）
        self._mem_lock = threading.Lock()
        # V4 配置：可选 RAG（结构化检索增强）+ 检索条数
        rag_conf = (config or {}).get("MemoryRAG", {}) or {}
        self.enable_rag = bool(rag_conf.get("enable_rag", True))
        self.rag_top_k = int(rag_conf.get("rag_top_k", 8))
        # __init__ 阶段 role_id 尚未确定（由各连接 init_memory 注入），不在此预加载

    # ---- 测试角色记忆隔离：路由到专用测试文件，不污染生产 .memory.yaml ----
    def _is_test_role(self, rid=None):
        rid = str(rid if rid is not None else getattr(self, "role_id", "") or "")
        return rid.startswith(_TEST_ROLE_PREFIXES)

    def _resolve_memory_paths(self, rid=None):
        """返回 (memory_path, memory_index_path)。测试角色路由到专用测试文件。"""
        base = get_project_dir() + "data/"
        if self._is_test_role(rid):
            return base + ".memory.test.yaml", base + ".memory_index.test.yaml"
        return base + ".memory.yaml", base + ".memory_index.yaml"

    # ---- 角色状态淘汰：防止进程单例 _state 只增不删导致长期内存泄漏 ----
    def _touch(self, rid):
        st = self._state.setdefault(rid, {"mem": "", "idx": {}, "llm": None})
        st["last_used"] = time.time()
        return st

    def _evict_stale(self):
        n = len(self._state)
        if n <= 50:
            return  # 小盘（常态）零开销，不遍历
        now = time.time()
        expired = [r for r, s in self._state.items()
                   if now - s.get("last_used", 0) > self._STATE_TTL]
        for r in expired:
            del self._state[r]
        if len(self._state) > self._MAX_ROLES:
            ordered = sorted(
                self._state.items(),
                key=lambda kv: kv[1].get("last_used", 0),
            )
            for r, _ in ordered[: len(self._state) - self._MAX_ROLES]:
                del self._state[r]

    def init_memory(
        self, role_id, llm, summary_memory=None, save_to_file=True, **kwargs
    ):
        super().init_memory(role_id, llm, **kwargs)
        self.save_to_file = save_to_file
        # 测试角色记忆读写隔离到专用测试文件(.memory.test.yaml)，
        # 既让 v4_memory/v4_rag 等功能测试正常验证，又不污染生产 .memory.yaml。
        # 每角色状态独立分桶，杜绝并发连接互踩。
        st = self._touch(role_id)
        st["llm"] = llm  # 按角色存 LLM，杜绝单例 self.llm 被并发连接覆盖
        self._current_role_id = role_id
        self._evict_stale()
        self.load_memory(summary_memory, role_id)

    def set_llm(self, llm):
        # 按当前角色把 LLM 存入 _state，杜绝单例 self.llm 被其他连接覆盖
        rid = getattr(self, "_current_role_id", None)
        if rid:
            self._touch(rid)
            self._state[rid]["llm"] = llm
        else:
            # 尚未 init_memory，退化为基类共享属性（兼容极端时序）
            super().set_llm(llm)

    def load_memory(self, summary_memory, role_id=None):
        role_id = role_id if role_id is not None else getattr(self, "role_id", "")
        st = self._state.setdefault(role_id, {"mem": "", "idx": {}})
        # api获取到总结记忆后直接返回（该角色使用外部总结，不落本地文件）
        if summary_memory or not self.save_to_file:
            st["mem"] = summary_memory or ""
            return

        mpath, mipath = self._resolve_memory_paths(role_id)
        all_memory = {}
        if os.path.exists(mpath):
            # 容错读取：历史文件可能含非 UTF-8 字节（早期版本/异常写入），
            # 用 errors="replace" 避免解码失败；若 YAML 结构已损坏则降级为空记忆，
            # 绝不因损坏文件导致整个服务启动崩溃。
            try:
                with open(mpath, "r", encoding="utf-8", errors="replace") as f:
                    all_memory = yaml.safe_load(f) or {}
                if not isinstance(all_memory, dict):
                    all_memory = {}
            except Exception as e:
                logger.bind(tag=TAG).warning(f"读取记忆文件失败，降级为空记忆: {e}")
                all_memory = {}
        st["mem"] = all_memory.get(role_id, "")

        # 加载结构化长期记忆索引（仅本角色）
        st["idx"] = {}
        if os.path.exists(mipath):
            try:
                with open(mipath, "r", encoding="utf-8") as f:
                    all_index = yaml.safe_load(f) or {}
                if isinstance(all_index, dict):
                    st["idx"] = all_index.get(role_id, {}) or {}
            except Exception as e:
                logger.bind(tag=TAG).warning(f"加载结构化记忆索引失败: {e}")
                st["idx"] = {}

    def _atomic_write_yaml(self, path, data):
        """原子写：先写临时文件再 os.replace，避免并发/中断读到半截内容。"""
        d = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise

    @contextlib.contextmanager
    def _xproc_lock(self, path):
        """跨进程锁：多实例共享同一数据卷（bind mount）时，串行化对同一 YAML 文件的读-改-写。

        用独立的 .lock 文件加 fcntl.flock，避免 _atomic_write_yaml 的 os.replace 换 inode
        导致文件锁失效。与进程内 self._mem_lock 配合：先拿进程内锁（同进程线程串行），
        再拿跨进程锁（不同实例串行），彻底消除「共享大 YAML 并发读-改-写互相覆盖」的坑。
        """
        lock_path = path + ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def save_memory_to_file(self, role_id):
        st = self._state.setdefault(role_id, {"mem": "", "idx": {}})
        mpath, _ = self._resolve_memory_paths(role_id)
        # 加锁串行化读-改-写，避免多会话并发保存互相覆盖/损坏
        with self._mem_lock:
            with self._xproc_lock(mpath):
                all_memory = {}
                if os.path.exists(mpath):
                    # 容错读取：历史文件可能含非 UTF-8 字节，用 errors="replace" 避免保存失败
                    try:
                        with open(mpath, "r", encoding="utf-8", errors="replace") as f:
                            raw_text = f.read()
                        try:
                            all_memory = yaml.safe_load(raw_text) or {}
                        except Exception:
                            # YAML 解析失败：尝试 JSON 兜底（部分历史文件为 JSON 格式）
                            all_memory = json.loads(raw_text) or {}
                        if not isinstance(all_memory, dict):
                            all_memory = {}
                    except Exception:
                        # 自愈：损坏文件先备份留存（便于人工恢复/排查），避免静默整文件清零
                        corrupt_bak = f"{mpath}.corrupt-{int(time.time())}"
                        try:
                            os.replace(mpath, corrupt_bak)
                            logger.bind(tag=TAG).warning(
                                f"记忆文件损坏无法解析，已备份至 {corrupt_bak}，本轮仅更新当前角色"
                            )
                        except Exception:
                            pass
                        all_memory = {}
                all_memory[role_id] = st["mem"]
                self._atomic_write_yaml(mpath, all_memory)

    async def save_memory(self, msgs, session_id=None, role_id=None):
        # 显式携带 role_id：单例被多连接共享，必须按角色定位状态，杜绝并发互踩串写
        role_id = role_id if role_id is not None else getattr(self, "role_id", None)
        if role_id is None:
            logger.bind(tag=TAG).warning("save_memory 缺少 role_id，跳过")
            return None
        st = self._state.setdefault(role_id, {"mem": "", "idx": {}})
        # 防御 set_llm 未调用场景（短会话/组件未初始化）
        if not st.get("llm"):
            logger.bind(tag=TAG).warning("LLM未绑定，跳过记忆保存")
            return None

        # 打印使用的模型信息
        model_info = getattr(st.get("llm"), "model_name", str(st.get("llm").__class__.__name__))
        logger.bind(tag=TAG).debug(f"使用记忆保存模型: {model_info}")
        api_key = getattr(st.get("llm"), "api_key", None)
        memory_key_msg = check_model_key("记忆总结专用LLM", api_key)
        if memory_key_msg:
            logger.bind(tag=TAG).error(memory_key_msg)
            # key 非法时直接放弃本次总结，避免用占位符/坏 key 发起请求导致编码崩溃
            return None

        if len(msgs) < 2:
            return None

        msgStr = ""
        for msg in msgs:
            content = msg.content

            # Extract content from JSON format if present (for ASR with emotion/language tags)
            try:
                if content and content.strip().startswith("{") and content.strip().endswith("}"):
                    data = json.loads(content)
                    if "content" in data:
                        content = data["content"]
            except (json.JSONDecodeError, KeyError, TypeError):
                # If parsing fails, use original content
                pass

            if msg.role == "user":
                msgStr += f"User: {content}\n"
            elif msg.role == "assistant":
                msgStr += f"Assistant: {content}\n"
        if st["mem"] and len(st["mem"]) > 0:
            msgStr += "历史记忆：\n"
            # 裁剪历史记忆输入，避免整段超长导致 LLM 输出被 max_tokens 截断而整轮记忆丢失
            msgStr += _trim_memory(st["mem"], max_cube=_TRIM_MAX_CUBE, max_chars=_TRIM_MAX_CHARS)

        # 当前时间（北京时间）
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 8 * 3600))
        msgStr += f"当前时间：{time_str}"

        if self.save_to_file:
            json_str = ""
            last_reason = ""
            # 卡顿修复：实测记忆膨胀后单次记忆总结约 50s，原 3 次重试 = 同步阻塞对话结束
            # 流程约 150s，设备长时间卡在「聆听中」。记忆是增强项、不是对话必需品：
            # 失败保留上一轮记忆即可，绝不应该用 150s 的用户可感卡顿去换一次记忆更新。
            # 故只调用一次，失败即降级保留旧记忆。
            for _attempt in range(1):
                try:
                    result = st["llm"].response_no_stream(
                        short_term_memory_prompt,
                        msgStr,
                        max_tokens=_SUMMARY_MAX_TOKENS,
                        temperature=_SUMMARY_TEMPERATURE,
                    )
                except Exception as e:
                    last_reason = f"LLM调用异常:{e}"
                    logger.bind(tag=TAG).warning(
                        f"记忆总结LLM调用异常(第{_attempt + 1}次): {e}"
                    )
                    continue
                js = extract_json_data(result)
                if js:
                    json_str = js
                    break
                last_reason = "未返回有效JSON"
                logger.bind(tag=TAG).warning(
                    f"记忆总结未返回有效JSON(第{_attempt + 1}次)，将重试"
                )
            if not json_str:
                logger.bind(tag=TAG).warning(
                    f"记忆总结重试后仍{last_reason or '失败'}，保留上一轮记忆（本轮不更新）"
                )
                return st["mem"]
            st["mem"] = _trim_memory(json_str, max_cube=_TRIM_MAX_CUBE, max_chars=_TRIM_MAX_CHARS)
            self.save_memory_to_file(role_id)
            # 从 LLM 总结结果解析出结构化长期记忆索引（零额外 LLM 调用）
            try:
                entries = self._rebuild_index(json_str)
                if entries is not None:
                    # 真进化(维度②E1): 持久事实合并——修"重建即遗忘"缺陷。
                    # 旧行为 st["idx"] = entries 整体替换：某轮 LLM 总结因上下文/长度
                    # 限制压缩掉的旧事实（生日/忌口/家人名字）从索引永久消失，
                    # 事实保留率随会话轮数单调下降。改为按 id upsert 合并，
                    # 旧事实保留、重复提及加权，容量超限按 (weight,ts) 驱逐。
                    st["idx"] = self._merge_index(st.get("idx"), entries)
                    self._save_index_to_file(role_id)
                    if METRICS:
                        METRICS.set_gauge(
                            "fuguoai_memory_structured_entries", len(entries)
                        )
                    logger.bind(tag=TAG).info(
                        f"结构化记忆索引已更新: {len(entries)} 条"
                    )
            except Exception as e:
                logger.bind(tag=TAG).warning(f"结构化记忆索引更新失败: {e}")
        else:
            # 当save_to_file为False时，调用Java端的聊天记录总结接口
            summary_id = session_id if session_id else role_id
            await generate_and_save_chat_summary(summary_id)
        logger.bind(tag=TAG).info(
            f"Save memory successful - Role: {role_id}, Session: {session_id}"
        )

        return st["mem"]

    # ------------------------------------------------------------------
    # V4：结构化长期记忆 + 可选 RAG（轻量词法检索增强）
    # ------------------------------------------------------------------
    def _mk_entry(self, etype, key, value, keywords):
        eid = hashlib.md5(f"{etype}:{key}".encode("utf-8")).hexdigest()[:12]
        return {
            "id": eid,
            "type": etype,
            "key": key,
            "value": value,
            "keywords": [str(k) for k in (keywords or [])],
            "ts": time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600)),
            "weight": 1.0,
        }

    def _rebuild_index(self, blob):
        """从 LLM 总结的 JSON 文本解析结构化条目（身份/偏好/事件/话题/事实）。"""
        try:
            data = json.loads(blob) if isinstance(blob, str) else blob
        except Exception:
            return None
        entries = []
        # 身份图谱：姓名 + 特征标记（个人化常驻）
        arch = data.get("时空档案", {}) or {}
        ident = arch.get("身份图谱", {}) or {}
        name = ident.get("现用名")
        if name:
            entries.append(self._mk_entry("identity", "name", f"姓名: {name}", [name]))
        traits = ident.get("特征标记", []) or []
        if traits:
            tv = "、".join(str(t) for t in traits)
            entries.append(self._mk_entry("preference", "traits", f"特征: {tv}", list(map(str, traits))))
        # 记忆立方：事件
        for ev in arch.get("记忆立方", []) or []:
            title = ev.get("事件", "") or ""
            ts = ev.get("时间戳", "") or ""
            val = f"{title} @{ts}" if ts else title
            entries.append(self._mk_entry("event", title, val, [title, str(ts)]))
        # 关系网络：高频话题
        rel = data.get("关系网络", {}) or {}
        topics = rel.get("高频话题", {}) or {}
        if isinstance(topics, dict):
            for t, c in topics.items():
                entries.append(self._mk_entry("topic", str(t), f"话题: {t}({c}次)", [str(t)]))
        # 待响应：紧急事项 / 潜在关怀（事实类）
        pend = data.get("待响应", {}) or {}
        for k in ("紧急事项", "潜在关怀"):
            for item in (pend.get(k) or []):
                entries.append(self._mk_entry("fact", str(item), f"{k}: {item}", [str(item)]))
        # 去重（按 id），保留首次出现
        seen = {}
        for e in entries:
            seen.setdefault(e["id"], e)
        return list(seen.values())

    def _merge_index(self, old, new):
        """按条目 id upsert 合并新旧索引，旧事实不再因单轮总结压缩而丢失。

        规则：
        - 同 id：新值覆盖（更新语义），ts 保留首次时间，weight +0.1 封顶 2.0
          （重复提及 = 更重要，检索排名随之提升）
        - 仅旧有：保留（这正是修复点——旧行为整体替换会丢掉它们）
        - 仅新有：加入
        - 超容量 _MAX_IDX_ENTRIES：identity/preference 常驻豁免，
          其余按 (weight, ts) 升序驱逐最轻最旧
        条目结构与旧版完全一致（id/type/key/value/keywords/ts/weight），
        文件格式不变，可无损回滚。
        """
        old_list = old if isinstance(old, list) else []
        new_list = new if isinstance(new, list) else []
        merged = {e["id"]: dict(e) for e in old_list if isinstance(e, dict) and e.get("id")}
        for e in new_list:
            if not (isinstance(e, dict) and e.get("id")):
                continue
            prev = merged.get(e["id"])
            if prev is not None:
                item = dict(e)
                # 保留首次时间戳；重复提及加权
                item["ts"] = prev.get("ts", item.get("ts"))
                item["weight"] = min(2.0, float(prev.get("weight", 1.0)) + 0.1)
                merged[e["id"]] = item
            else:
                merged[e["id"]] = dict(e)
        entries = list(merged.values())
        if len(entries) > self._MAX_IDX_ENTRIES:
            pinned = [e for e in entries if e.get("type") in ("identity", "preference")]
            others = [e for e in entries if e.get("type") not in ("identity", "preference")]
            keep_n = max(0, self._MAX_IDX_ENTRIES - len(pinned))
            others.sort(key=lambda e: (float(e.get("weight", 1.0)), str(e.get("ts", ""))), reverse=True)
            entries = pinned + others[:keep_n]
        return entries

    def _save_index_to_file(self, role_id):
        st = self._state.setdefault(role_id, {"mem": "", "idx": {}})
        _, mipath = self._resolve_memory_paths(role_id)
        # 加锁 + 原子写，避免并发保存同一索引文件造成损坏
        with self._mem_lock:
            with self._xproc_lock(mipath):
                all_index = {}
                if os.path.exists(mipath):
                    try:
                        with open(mipath, "r", encoding="utf-8", errors="replace") as f:
                            all_index = yaml.safe_load(f) or {}
                        if not isinstance(all_index, dict):
                            all_index = {}
                    except Exception:
                        all_index = {}
                all_index[role_id] = st["idx"]
                self._atomic_write_yaml(mipath, all_index)

    @staticmethod
    def _tokenize(text):
        if not text:
            return []
        text = str(text).lower()
        # CJK 单字 + 二元组，覆盖中文检索
        cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
        tokens = list(cjk)
        for i in range(len(cjk) - 1):
            tokens.append(cjk[i : i + 2])
        # 英文/数字词
        tokens += re.findall(r"[a-z0-9]+", text)
        return [t for t in tokens if len(t) > 0]

    def _score_entries(self, entries, query):
        q_tokens = self._tokenize(query)
        if not q_tokens:
            return []
        type_weight = {"event": 1.0, "fact": 1.2, "topic": 0.9, "identity": 1.0, "preference": 1.0}
        # 双字/词权重高于单字，抑制单字噪声匹配（如"得""了"）
        def tok_weight(t):
            return 1.0 if len(t) >= 2 else 0.3
        scored = []
        for e in entries:
            blob = (e.get("value", "") + " " + " ".join(e.get("keywords", []))).lower()
            s = 0.0
            for t in q_tokens:
                if t and t in blob:
                    s += tok_weight(t)
            if s <= 0:
                continue
            s *= type_weight.get(e["type"], 1.0)
            # 真进化(维度②E1): 重复提及的事实 weight 更高（合并时累加），检索排名提升
            s *= float(e.get("weight", 1.0))
            scored.append((s, e))
        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored]

    async def query_memory(self, query: str, role_id=None) -> str:
        role_id = role_id if role_id is not None else getattr(self, "role_id", None)
        if role_id is None:
            return ""
        st = self._state.setdefault(role_id, {"mem": "", "idx": {}})
        # RAG 关闭时退化为原行为：返回完整记忆块
        if not self.enable_rag:
            return st["mem"]
        idx = st["idx"]
        if not idx:
            # 结构化索引为空（如首次会话）时退化为完整块，保证不丢记忆
            return st["mem"]
        # 个人化常驻：身份/偏好始终注入，确保助手认识用户
        pinned = [e for e in idx if e["type"] in ("identity", "preference")]
        others = [e for e in idx if e["type"] not in ("identity", "preference")]
        topk = self._score_entries(others, query)[: self.rag_top_k]
        chosen = pinned + topk
        if not chosen:
            return st["mem"]
        text = "[长期记忆]\n" + "\n".join(f"- ({e['type']}) {e['value']}" for e in chosen)
        if METRICS:
            METRICS.inc("fuguoai_memory_rag_queries_total")
            if topk:
                METRICS.inc("fuguoai_memory_rag_hits_total")
            METRICS.set_gauge("fuguoai_memory_structured_entries", len(idx))
        return text
