"""地方文旅知识库（TourismKB）。

设计动机
--------
复用 FamilyRAG 的检索引擎范式（可插拔 embedding / 向量自动迁移 / 余弦 top-k /
进程单例 / 内存缓存 + 锁外落盘），但面向「文旅文档」而非「家庭事实」：

- 存储单元是「文档块」(doc) 而非 fact；每条含 type/title/text/vec/metadata；
- 分区键由 device_id 泛化为 kb_id（按城市/景区，如 "hubei"）；
- 检索在语义召回基础上叠加**专名/关键词混合检索**（景点名精确匹配优先 boost），
  解决纯向量对「故宫 vs 恭王府」这类专有名词区分力不足的问题；
- 提供 query()：检索 → LLM 基于 <知识库> 上下文合成回答，并附来源景点名。

与 FamilyRAG 共用 fuguoai_core.providers.embedding 工厂与 LLM 工厂，零额外基础设施。
"""

import copy
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from fuguoai_config.config_loader import get_project_dir
from fuguoai_config.logger import setup_logging
from fuguoai_core.providers.embedding import create_embedding_instance
from fuguoai_core.utils.llm import create_instance
from fuguoai_core.utils.metrics import METRICS

TAG = __name__
logger = setup_logging()

_KB_TYPES = {
    "scenic", "food", "route", "stay",
    "transport", "festival", "info", "other",
    # 文化向：逸闻/典故/传说/民俗 与 诗词歌赋（2026-09-23 为文旅"故事诗词"素材新增）
    "culture", "poem",
    # 实用信息向：票务与开放时间 / 最佳季节 / 安全贴士 / 购物特产
    # （2026-09-25 实用信息批新增。⚠️ add_doc 有本白名单校验：不在表内的 type 一律回落
    #  "other"（add_doc:279），故新增类别必须先在此登记，否则类别标注会静默失效。
    #  type 仅用于分类统计与来源标注展示，不参与检索/rerank 排序。）
    "ticket", "season", "tip", "shop",
}

# ---- 数值精度 faithfulness：单位归一 ----
# 每个单位 token → (class, 类内基准单位倍率)。先做归一化再容差比对，
# 解决「公顷↔万㎡」「公里↔km」「平方米↔平米」等同义/异写单位导致的不匹配误报。
# 仅覆盖标准化、KB 中书写一致的单位（面积/长度/质量/温度/年份）；
# 不覆盖「处/个/家/条」等近义繁多的计数单位（易误报，留作后续升级）。
_NUM_UNIT_CLASS = {
    # —— 面积（基准：平方米 m²）——
    "平方公里": ("area", 1e6),
    "平方千米": ("area", 1e6),
    "km²": ("area", 1e6),
    "公顷": ("area", 1e4),
    "万平方米": ("area", 1e4),
    "万㎡": ("area", 1e4),
    "亩": ("area", 2000.0 / 3.0),   # 1 亩 ≈ 666.67 m²
    "平方米": ("area", 1.0),
    "平米": ("area", 1.0),
    "平方公尺": ("area", 1.0),
    # —— 长度（基准：米 m）——
    "公里": ("len", 1e3),
    "千米": ("len", 1e3),
    "km": ("len", 1e3),
    "米": ("len", 1.0),
    "m": ("len", 1.0),
    "厘米": ("len", 0.01),
    "公分": ("len", 0.01),
    "cm": ("len", 0.01),
    "毫米": ("len", 0.001),
    "mm": ("len", 0.001),
    # —— 质量（基准：千克 kg）——
    "吨": ("mass", 1e3),
    "千克": ("mass", 1.0),
    "公斤": ("mass", 1.0),
    "kg": ("mass", 1.0),
    "克": ("mass", 1e-3),
    "g": ("mass", 1e-3),
    # —— 温度（基准：摄氏度）——
    "摄氏度": ("temp", 1.0),
    "℃": ("temp", 1.0),
    "度": ("temp", 1.0),
}
_NUM_CLASS_CN = {"area": "面积", "len": "长度", "mass": "质量", "temp": "温度", "year": "年份"}
_UNIT_ALT = "|".join(sorted(_NUM_UNIT_CLASS.keys(), key=len, reverse=True))
_NUM_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万|亿)?\s*(" + _UNIT_ALT + r")")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def _extract_numbers(text):
    """从文本抽取数值 + 单位元信息。返回 [{value(归一化), cls, raw, unit}, ...]。"""
    if not text:
        return []
    out = []
    for m in _NUM_UNIT_RE.finditer(text):
        num = float(m.group(1))
        mult = m.group(2)
        unit = m.group(3)
        cls, base = _NUM_UNIT_CLASS[unit]
        val = num * base
        if mult == "万":
            val *= 1e4
        elif mult == "亿":
            val *= 1e8
        out.append({"value": val, "cls": cls, "raw": m.group(0), "unit": unit})
    for m in _YEAR_RE.finditer(text):
        y = int(m.group(0))
        out.append({"value": float(y), "cls": "year", "raw": m.group(0), "unit": "年"})
    return out

# ---- 实体级归属校验（张冠李戴护栏）纯逻辑 ----
# 这些函数零 LLM / 零重依赖，单独可单测（见 tools/test_entity_attrib.py）。

# 行政/经济区复合词后缀：若景点名后立即接这些 token，说明它是更大地名的组成部分
# （如「东湖高新区」），不应作为独立景点被匹配，避免把复合行政区里的专名误抽成 POI
# （例：把「武汉东湖高新区」里的「东湖」当成《东湖》景区，进而引发数字就近归属误报）。
_ADMIN_COMPOUND_TOKENS = (
    "高新区", "开发区", "新区", "经济区", "科技园", "产业园",
    "商务区", "保税区", "工业园", "示范区", "新城",
)


def _num_tol(cls, a, b):
    """同单位数值容差：年份精确；其余按 15%（面积/长度/质量/温度）并设绝对下限，避免四舍五入误报。"""
    m = max(abs(a), abs(b), 1e-9)
    if cls == "year":
        return 0.5
    return max(0.15 * m, 1.0)


# 紧邻归属窗口（字符）：数字起点距其前面最近 POI 名结尾 ≤ 此值，视为「该数字紧邻修饰此 POI」。
# 用于 (e) 紧邻归属——消除「排骨藕汤…约30厘米」被误判为恩施大峡谷张冠李戴类误报。
_TIGHT_WIN = 12


def _canon_spot(title):
    """同源多条目归一为主标题：把《荆州古城·搜狐旅游资料》《荆州古城（湖北）》
    这类同一景点的多来源条目归一为「荆州古城」，使其在实体归属时被视作同一 POI，
    避免同源条目互判为「张冠李戴」的误报。"""
    canon = re.split(r"[·•・·（）()【】\[\]\-\—~～/]", title)[0].strip()
    return canon or title


def _entity_attribution_core(answer, title_numbers, retrieved_canon=None):
    """实体级归属校验纯函数（详见原 _entity_attribution_check 文档）。

    三处改进（相对早期版本）：
      (a) 同景点多条目归一：数字是否「属于本 POI」按 canon spot 合并判断，同源条目不再互判；
      (b) 复合行政区专名不误抽：POI 名后立即接行政/经济区后缀（如「东湖高新区」）时跳过该匹配；
      (c) 就近归属优先取数字「前面」的 POI（中文修饰语多后置其头名词），并列再取 gap 最小者。
      (d) 自归属优先（C3-b）：数字前就近出现、且自身就含该数值的 POI 即真正归属，跳过判罚，
          避免「张居正街…全长约1公里」被「荆州古城内」抢走归属而误判张冠李戴。
      (e) 紧邻归属（C3-c）：数字紧邻其归属 POI（gap≤_TIGHT_WIN）且该 POI 自身数据不矛盾时，
          视为对该 POI 的上下文描述，不再跨条目判罚——消除「排骨藕汤…约30厘米」被误判为恩施
          大峡谷张冠李戴（恩施仅因巧合同数且被检索到）。仅当紧邻 POI 自身数据【矛盾】才保留
          判罚（黄鹤楼说成30米而其真实51.4米→仍命中），不影响真实跨条目检测。
      (f) 主体无此类记录（C3-c）：主体 POI 自身未收录该计量类别时不判张冠李戴——洪湖莲藕莲
          藕段「30厘米」与荆州古城墙厚「30厘米」巧合同数，主体无长度记录则无法断定错配，跳过。

    retrieved_canon（C3 新增，可选）：本次实际喂给 LLM 的检索条目 canon 集合。若提供且非空，
    则「张冠李戴」只在该数字的「真正归属 POI（other）」也被检索到时才报——避免把答案中
    随口提到的数字（如描述藕段「约30厘米」）与**未检索**条目的相同数字（恩施大峡谷长度）
    误判为跨条目错配（2026-09-26 实测随机 caveat 根因之一）。传 None/空集则保持旧行为。
    """
    if not answer or not title_numbers:
        return []
    # (a) 同景点多条目归一
    canon_of = {}
    canon_numbers = {}
    for t, nums in title_numbers.items():
        c = _canon_spot(t)
        canon_of[t] = c
        canon_numbers.setdefault(c, [])
        canon_numbers[c].extend(nums)
    # (b) 长名优先匹配；剔除复合行政区误抽
    titles = sorted(title_numbers.keys(), key=len, reverse=True)
    poi_spans = []
    for t in titles:
        for m in re.finditer(re.escape(t), answer):
            ps, pe = m.start(), m.end()
            after = answer[pe:pe + 4]
            if any(after.startswith(tok) for tok in _ADMIN_COMPOUND_TOKENS):
                continue
            poi_spans.append((ps, pe, t))
    if not poi_spans:
        return []
    flags = []
    seen = set()
    for nm in _extract_numbers(answer):
        if nm["cls"] == "year":
            continue
        n_raw = nm["raw"]
        idx = answer.find(n_raw)
        if idx < 0:
            continue
        ns, ne = idx, idx + len(n_raw)
        # (d) 自归属优先（C3-b 新增）：若数字【前面】就近出现、且自身就含该数值的 POI，
        # 则它即真正归属（如「张居正街：…全长约1公里」中 1公里 本就属于张居正街，不应因
        # 「位于荆州古城内」紧邻而被误判为张冠李戴）。找到自归属即跳过，不影响真实跨条目
        # 错配（黄鹤楼/晴川阁、首义广场/中山公园）命中。仅在数字前确有含该数的 POI 时生效。
        self_owner = False
        for (ps, pe, t) in poi_spans:
            if pe <= ns and any(
                o["cls"] == nm["cls"]
                and abs(o["value"] - nm["value"]) <= _num_tol(nm["cls"], nm["value"], o["value"])
                for o in canon_numbers[canon_of[t]]
            ):
                self_owner = True
                break
        if self_owner:
            continue
        # (c) 就近归属：优先数字前面最近的 POI，并列再取 gap 最小
        best = None
        for (ps, pe, t) in poi_spans:
            before = pe <= ns
            gap = max(0, max(ps - ne, ns - pe))
            key = (0 if before else 1, gap, ps)
            if best is None or key < best[0]:
                best = (key, ps, pe, t)
        t = best[3]
        c_t = canon_of[t]
        c_t_cls_nums = [o for o in canon_numbers[c_t] if o["cls"] == nm["cls"]]
        # (f) 主体 POI 无此类记录 → 无法断定该数字错配（可能只是本 POI 未收录该计量；如洪湖莲藕
        # 莲藕段「30厘米」被误判为荆州古城墙厚「30厘米」），跳过判罚，消除此类随机 caveat。
        if not c_t_cls_nums:
            continue
        if any(abs(o["value"] - nm["value"]) <= _num_tol(nm["cls"], nm["value"], o["value"])
               for o in c_t_cls_nums):
            continue  # 该数值属于本 spot（含同源多条目）自身，正常
        # (e) 紧邻归属（C3-c）：数字紧邻其归属 POI（gap≤_TIGHT_WIN）且该 POI 自身数据不矛盾时，
        # 视为对该 POI 的上下文描述，不再跨条目判罚。仅当紧邻 POI 自身数据【矛盾】才保留判罚
        # （如黄鹤楼说成30米而其真实51.4米→仍命中，属真实错配），不影响真实跨条目检测。
        gap_t = max(0, ns - best[2])
        if gap_t <= _TIGHT_WIN:
            x_contra = any(abs(o["value"] - nm["value"]) > _num_tol(nm["cls"], nm["value"], o["value"])
                           for o in c_t_cls_nums)
            if not x_contra:
                continue
        for other, onums in title_numbers.items():
            if other == t or canon_of[other] == c_t:
                continue  # 同景点不算张冠李戴
            # C3：真正归属 POI 未被检索到 → 答案无从误引它，纯属巧合同数，不报（消除随机 caveat）
            if retrieved_canon and canon_of[other] not in retrieved_canon:
                continue
            if any(o["cls"] == nm["cls"]
                   and abs(o["value"] - nm["value"]) <= _num_tol(nm["cls"], nm["value"], o["value"])
                   for o in onums):
                k = (t, n_raw, other)
                if k in seen:
                    break
                seen.add(k)
                flags.append(
                    "数字「%s」出现在《%s》附近，但实为《%s》的「%s」数据，疑似张冠李戴"
                    % (n_raw, t, other, _NUM_CLASS_CN[nm["cls"]])
                )
                break
    return flags


# LLM 结构化抽取的提示词（ingest_raw 用）
_INGEST_PROMPT = """你是文旅信息结构化抽取器。请从下面的原始资料中抽取与旅游相关的条目，
只输出一个 JSON 数组，每个元素为：
{{"type":"类型","title":"条目名","text":"一段连贯的中文描述(80-200字)","metadata":{{"city":"城市","tags":["关键词"]}}}}
类型从以下选一：scenic(景点)/food(美食)/route(路线)/stay(住宿)/transport(交通)/festival(节庆)/culture(逸闻故事·典故传说·民俗)/poem(诗词歌赋)/info(实用信息)/other。
若资料中没有任何旅游相关信息，输出空数组 []。不要输出任何解释文字，只输出 JSON 数组。
原始资料：
{raw}
输出："""


def _norm_kb_id(kb_id):
    return re.sub(r"[^a-z0-9_一-鿿]+", "_", (kb_id or "").strip().lower()).strip("_") or "default"


def _bigrams(s):
    """中文/混合字符串的字符 bigram 集合，用于关键词重叠打分。"""
    s = re.sub(r"\s+", "", s or "")
    return set(s[i:i + 2] for i in range(len(s) - 1))


# 城市感知 rerank 用的湖北行政区划词表（从 query 抽取城市意图；对跨城文档降权）。
# 顺序：长地名优先命中（恩施土家族苗族自治州 应在 恩施 之前）。
_HUBEI_PREFECTURES = [
    "恩施土家族苗族自治州", "神农架", "仙桃", "潜江", "天门", "武汉", "黄石",
    "十堰", "宜昌", "襄阳", "鄂州", "荆门", "孝感", "荆州", "黄冈", "咸宁", "随州",
]
_PROVINCE_LEVEL = {"湖北", "中国", "全国", "华夏", "本省"}
_CITY_ALIASES = {  # query 中的区县/旧称 -> 标准地级市
    "武昌": "武汉", "汉口": "武汉", "汉阳": "武汉", "江夏": "武汉", "黄陂": "武汉",
    "新洲": "武汉", "蔡甸": "武汉", "江汉": "武汉", "东西湖": "武汉", "青山": "武汉",
    "襄樊": "襄阳", "荆沙": "荆州",
}

# C5 跨域过滤词表：主要「非湖北」城市/地标。若 query 显式提到其中之一且融合分处中低段
# （< high_conf），说明命中只是语义撞词（如「上海迪士尼」撞「武当山·游玩攻略」），应走兜底
# （通用 LLM 答通识），避免用不相关湖北文档产出张冠李戴的错误答案。
# 注：此为「保守拦截」——命中即降级兜底（兜底路径用通用 LLM，仍能正确回答通识），不会丢信息；
# 不在表内的冷门跨域词可能仍走 RAG（由阈值兜底），属已知边界。
_CROSS_REGION_PLACES = (
    "上海", "北京", "广州", "深圳", "三亚", "海南", "西藏", "拉萨", "西安", "成都",
    "杭州", "南京", "重庆", "云南", "昆明", "桂林", "苏州", "天津", "青岛", "厦门",
    "大连", "长沙", "郑州", "哈尔滨", "沈阳", "济南", "福州", "贵阳", "南宁", "兰州",
    "乌鲁木齐", "呼和浩特", "银川", "西宁", "香港", "澳门", "台湾", "纽约", "巴黎",
    "东京", "伦敦", "迪士尼", "故宫", "布达拉宫", "外滩", "长城", "兵马俑", "西湖",
)


def _clean_doc(d):
    """去掉向量，返回可对外暴露的条目。"""
    return {
        "id": d.get("id"),
        "type": d.get("type", "other"),
        "title": d.get("title", ""),
        "text": d.get("text", ""),
        "metadata": d.get("metadata") or {},
        "ts": d.get("ts", 0),
    }


class TourismKB:
    def __init__(self, config):
        cfg = (config.get("TourismKB", {}) or {}) if isinstance(config, dict) else {}
        self.enabled = bool(cfg.get("enabled", False))
        self.top_k = int(cfg.get("top_k", 5))

        emb_cfg = (cfg.get("embedding") or {}) or {}
        emb_type = emb_cfg.get("type", "local-st")
        try:
            self.embedding = create_embedding_instance(emb_type, emb_cfg)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"embedding 初始化失败，降级 local: {e}")
            self.embedding = create_embedding_instance("local", {})
        self._emb_type = getattr(self.embedding, "type_key", emb_type)

        # 混合检索权重：语义 0.65 / 关键词 0.35
        hybrid = (cfg.get("hybrid") or {}) or {}
        self.semantic_weight = float(hybrid.get("semantic_weight", 0.65))

        sel_llm = (config.get("selected_module", {}) or {}).get("LLM") or "OpenAILLM"

        qcfg = (cfg.get("query") or {}) or {}
        self.query_enabled = bool(qcfg.get("enabled", True))
        self.query_llm_block = qcfg.get("llm") or sel_llm
        self.query_timeout = float(qcfg.get("timeout", 15.0))
        # 检索未命中兜底阈值：最佳融合分低于此值视为未命中 KB，改用 LLM 通用知识即时生成。
        # 调高(如 2.0) = 几乎全走兜底；调低(如 -1) = 关闭兜底、回到严格 RAG。
        # 兜底阈值 0.40：放宽以让「KB 有答案但融合分中等」的查询回归 RAG（避免被踢去走通用 LLM 绕开 KB）。
        # 安全性由城市感知 reranker 保证（跨城文档已降权，不会因阈值放宽而污染）。
        # KB 真缺内容（如滑雪场/某城美食，融合分 <0.40）仍保留兜底，不会错误拉入。
        self.fallback_threshold = float(qcfg.get("fallback_threshold", 0.40))
        # 高置信阈值：≥此值视为强命中，走严格 RAG 不加额外约束；
        # 介于 fallback_threshold 与 high_conf_threshold 之间为「中等相关」，
        # 走 RAG 但加「仅依据明确支持的内容作答」强约束，并在返回中标记 confidence=medium。
        self.high_conf_threshold = float(qcfg.get("high_conf_threshold", 0.70))

        # 城市感知 rerank（检索质量升级）：跨城（具体其他市）降权 / 同城加权 / 专名精确命中加权。
        # 阈值判定仍用原始融合分，rerank 只重排喂给 LLM 的 top_k，不改变兜底/置信分档行为。
        rcfg = (cfg.get("rerank") or {}) or {}
        self.rerank_enabled = bool(rcfg.get("enabled", True))
        self.rerank_city_boost = float(rcfg.get("city_boost", 0.20))
        self.rerank_city_penalty = float(rcfg.get("city_penalty", 0.30))
        self.rerank_name_boost = float(rcfg.get("name_boost", 0.15))
        self.rerank_candidate_k = int(rcfg.get("candidate_k", 20))
        # faithfulness 护栏：答案提及的 KB 内 POI 若不在检索上下文，追加「仅供参考」警示（防虚构/跨城张冠李戴）。
        fcfg = (qcfg.get("faithfulness") or {}) or {}
        self.faith_enabled = bool(fcfg.get("enabled", True))
        self.faith_caveat = bool(fcfg.get("caveat", True))
        # 数值精度护栏（检索质量升级·续）：答案中的面积/长度/质量/温度/年份若与检索上下文
        # 记录的同单位数值不符（或引入了 KB 未检索到的计量类别），追加「仅供参考」警示（防数字美化/编造）。
        self.faith_numeric_enabled = bool(fcfg.get("numeric_enabled", True))
        # 实体级归属校验（检索质量升级·续续）：答案中某 POI 名附近出现的面积/长度/质量/温度数值，
        # 若不属于该 POI 自身策展数字、却匹配 KB 中另一 POI 的策展数字，判定为跨条目数字错配
        # （张冠李戴），追加「仅供参考」警示（防"把 A 条目的数字安到 B 条目"）。
        self.faith_entity_enabled = bool(fcfg.get("entity_enabled", True))
        # 语义级事实一致性复核（检索质量升级·续续续）：三道正则护栏抓不到「上下文里没有的
        # 开放时间/票价/路线细节被写进答案」这类语义幻觉，由 LLM 在合成后做一次低成本审查。
        # 默认关闭：不调用则不产生额外 LLM 成本、不改变任何答案（零回归）。
        self.faith_llm_review_enabled = bool(fcfg.get("llm_review_enabled", False))

        icfg = (cfg.get("ingest") or {}) or {}
        self.ingest_enabled = bool(icfg.get("enabled", True))
        self.ingest_llm_block = icfg.get("llm") or sel_llm
        self.ingest_timeout = float(icfg.get("timeout", 30.0))

        self._dir = os.path.join(get_project_dir(), "data", "tourism_kb")
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tourkb")
        # 内存缓存 + 锁外落盘（同 FamilyRAG 加固范式）
        self._mem = {}
        self._io_lock = threading.RLock()
        self._migrate_lock = threading.Lock()
        self._migrate_ts = {}
        self._test_llm = None
        self._llm_cache = {}
        self._llm_lock = threading.Lock()
        # 检索索引缓存：kb_id -> {"sig", "mat"(已L2归一化的文档向量矩阵), "tbg", "xbg"}
        # 实体归属校验的「标题→数字」缓存：kb_id -> {"sig", "tn"}
        # （2026-09-26 性能优化：避免每 doc 重建 query 向量、每问重做全文 bigram/
        #    _extract_numbers 分词——这三处都曾是「每问重复扫全库」）
        self._retr_index = {}
        self._title_num_cache = {}
        # 启动预热开关（默认关）：见 warmup()。开启后把「进程级一次性开销」从首个真实
        # 请求挪到服务启动阶段，代价是启动即占用 embedding 模型内存（未使用文旅 KB 时是浪费）。
        wcfg = (cfg.get("warmup") or {}) or {}
        self.warmup_enabled = bool(wcfg.get("enabled", False))
        self.warmup_kb_ids = list(wcfg.get("kb_ids") or ["hubei"])

    # ---- 测试注入点 ----
    def _set_test_llm(self, inst):
        self._test_llm = inst

    # ---- 存储 ----
    def _path(self, kb_id):
        return os.path.join(self._dir, f"{_norm_kb_id(kb_id)}.json")

    def _load(self, kb_id):
        p = self._path(kb_id)
        try:
            if not os.path.exists(p):
                return {"docs": []}
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"docs": []}
            return data
        except Exception as e:
            logger.bind(tag=TAG).warning(f"文旅知识读取失败(降级空): {e}")
            return {"docs": []}

    def _save(self, kb_id, data):
        p = self._path(kb_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)

    def _get_mem(self, kb_id):
        with self._io_lock:
            data = self._mem.get(kb_id)
            if data is None:
                data = self._load(kb_id)
                self._mem[kb_id] = data
            return data

    # ---- 录入 ----
    def add_doc(self, kb_id, doc_type, title, text, metadata=None):
        """录入/更新一条文旅文档块。相同 (type+title+text) 视为同一条。返回清洁条目或 None。"""
        did = _norm_kb_id(kb_id)
        if not self.enabled:
            return None
        title = (title or "").strip()
        text = (text or "").strip()
        if len(text) < 4 or len(title) < 1:
            return None
        doc_type = doc_type if doc_type in _KB_TYPES else "other"
        fid = hashlib.sha1(f"{doc_type}|{title}|{text}".encode("utf-8")).hexdigest()[:16]
        try:
            vec = self.embedding.embed([text])[0]
        except Exception as e:
            logger.bind(tag=TAG).warning(f"embedding 失败(跳过录入): {e}")
            return None
        if not vec or all(float(x) == 0.0 for x in vec):
            logger.bind(tag=TAG).warning("embedding 返回零向量(跳过录入)")
            return None
        meta = dict(metadata or {})
        now = int(time.time())
        try:
            with self._io_lock:
                data = self._get_mem(did)
                docs = data.setdefault("docs", [])
                existing = next((d for d in docs if d.get("id") == fid), None)
                if existing:
                    existing["ts"] = now
                    existing["type"] = doc_type
                    existing["title"] = title
                    existing["text"] = text
                    existing["metadata"] = meta
                    existing["vec"] = vec
                    existing["emb_type"] = self._emb_type
                    doc = existing
                else:
                    doc = {
                        "id": fid,
                        "type": doc_type,
                        "title": title,
                        "text": text,
                        "metadata": meta,
                        "vec": vec,
                        "emb_type": self._emb_type,
                        "ts": now,
                    }
                    docs.append(doc)
                snapshot = copy.deepcopy(data)
            self._save(did, snapshot)
            try:
                METRICS.set_gauge("fuguoai_tourism_kb_docs", len(docs),
                                  labels={"kb_id": did})
            except Exception:
                pass
            return _clean_doc(doc)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"文旅知识写入失败: {e}")
            return None

    def remove_doc(self, kb_id, doc_id):
        did = _norm_kb_id(kb_id)
        try:
            with self._io_lock:
                data = self._get_mem(did)
                docs = data.get("docs", [])
                new = [d for d in docs if d.get("id") != doc_id]
                if len(new) == len(docs):
                    return False
                data["docs"] = new
                snapshot = copy.deepcopy(data)
            self._save(did, snapshot)
            return True
        except Exception as e:
            logger.bind(tag=TAG).warning(f"文旅知识删除失败: {e}")
            return False

    def clear(self, kb_id):
        did = _norm_kb_id(kb_id)
        try:
            with self._io_lock:
                data = self._get_mem(did)
                n = len(data.get("docs", []))
                data["docs"] = []
                snapshot = copy.deepcopy(data)
            self._save(did, snapshot)
            return n
        except Exception as e:
            logger.bind(tag=TAG).warning(f"文旅知识清空失败: {e}")
            return 0

    def list_docs(self, kb_id):
        try:
            data = self._get_mem(_norm_kb_id(kb_id))
            docs = [_clean_doc(d) for d in data.get("docs", [])]
            docs.sort(key=lambda x: x.get("ts", 0), reverse=True)
            return docs
        except Exception:
            return []

    # ---- 向量迁移（切换 embedding 后端时） ----
    def _ensure_vectors(self, kb_id, data):
        docs = data.get("docs", []) if isinstance(data, dict) else []
        if not docs:
            return data
        now = time.time()
        with self._migrate_lock:
            last = self._migrate_ts.get(kb_id, 0)
            if now - last < 60:
                return data
            self._migrate_ts[kb_id] = now
        changed = False
        for d in docs:
            if d.get("emb_type") == self._emb_type:
                continue
            text = (d.get("text") or "").strip()
            if not text:
                continue
            try:
                vec = self.embedding.embed([text])[0]
            except Exception as e:
                logger.bind(tag=TAG).warning(f"向量迁移 embed 失败(跳过): {e}")
                vec = None
            if not vec or all(float(v) == 0.0 for v in vec):
                continue
            d["vec"] = vec
            d["emb_type"] = self._emb_type
            changed = True
        if changed:
            try:
                with self._io_lock:
                    snapshot = copy.deepcopy(data)
                self._save(kb_id, snapshot)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"向量迁移落盘失败: {e}")
        return data

    # ---- 检索 ----
    # ⚠️ 2026-09-26 删除死代码 `_sim`：原为逐条算余弦，检索向量化后已无调用点
    #    （全仓 grep 仅剩定义）。语义相似度现由 _retrieval_index 的矩阵点积统一计算。
    def _keyword_score(self, query, doc, qbg, title_bg=None, text_bg=None):
        """关键词 bigram 重叠打分。title_bg/text_bg 可由调用方传入缓存值以复用（避免重复分词）。"""
        if not qbg:
            return 0.0
        if title_bg is None:
            title_bg = _bigrams(doc.get("title", ""))
        if text_bg is None:
            text_bg = _bigrams(doc.get("text", ""))
        title_hit = len(qbg & title_bg) / len(qbg)
        text_hit = len(qbg & text_bg) / len(qbg)
        # 标题命中权重更高（专名精确匹配优先）
        return min(1.0, title_hit * 0.7 + min(text_hit, 1.0) * 0.3)

    def _docs_signature(self, docs):
        """文档集签名，用于各类「按库内容缓存」的失效判定。

        = (条数, 全部 doc id, 全部 emb_type)：
          - doc id = sha1(type|title|text)，标题/正文/类型一变 id 就变 ⇒ 覆盖 add_doc 与删改；
          - emb_type 覆盖 `_ensure_vectors` 只换 vec、不换 id 的情形（否则会读到脏缓存）。
        """
        return (
            len(docs),
            tuple(d.get("id") or "" for d in docs),
            tuple(d.get("emb_type") or "" for d in docs),
        )

    def _retrieval_index(self, kb_id, docs):
        """检索索引缓存：文档向量矩阵（行已 L2 归一化）+ 标题/正文 bigram 集合。

        ⚠️ 为什么需要（2026-09-26 实测）：原实现对每个 doc 都调一次 `_sim`，而 `_sim`
           **每次都重新构造并归一化 query 向量**；`_keyword_score` 又对每篇正文
           **每问重新做一次全文 bigram 分词**。302 条 × 全文 × 每问 = 纯重复计算，
           实测 33ms/次检索。改为一次性建矩阵 + 缓存 bigram 后，单次矩阵点积算完
           全部相似度（数值等价，快一个量级）。

        ⚠️ 缓存失效签名 = (条数, 全部 doc id, 全部 emb_type)：
           - doc id = sha1(type|title|text)，正文/标题/类型一变 id 就变 ⇒ 覆盖 add_doc 与删改；
           - emb_type 覆盖 `_ensure_vectors` 只换 vec、不换 id 的情形。
           任一变动 ⇒ 签名变化 ⇒ 重建索引，不会读到脏缓存。
        """
        sig = self._docs_signature(docs)
        ent = self._retr_index.get(kb_id)
        if ent is not None and ent.get("sig") == sig:
            return ent

        dim = 0
        for d in docs:
            v = d.get("vec") or []
            if len(v) > dim:
                dim = len(v)
        mat = np.zeros((len(docs), dim), dtype=np.float32)
        for i, d in enumerate(docs):
            v = d.get("vec") or []
            # 长度不符或空向量的文档保持零行 ⇒ 点积为 0，与原 _sim 返回 0.0 一致
            if dim and len(v) == dim:
                mat[i] = np.asarray(v, dtype=np.float32)
        if dim:
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0.0] = 1.0
            mat = mat / norms
        ent = {
            "sig": sig,
            "mat": mat,
            "tbg": [_bigrams(d.get("title", "")) for d in docs],
            "xbg": [_bigrams(d.get("text", "")) for d in docs],
        }
        self._retr_index[kb_id] = ent
        return ent

    def hybrid_retrieve(self, kb_id, query, top_k=None):
        """混合检索：语义余弦 + 关键词 bigram 重叠，加权融合后返回 top_k。

        返回 [(doc_clean_dict, semantic_sim, keyword_score), ...]，按融合分降序。
        """
        if not self.enabled or not query:
            return []
        top_k = top_k or self.top_k
        did = _norm_kb_id(kb_id)
        data = self._get_mem(did)
        data = self._ensure_vectors(did, data)
        docs = data.get("docs", [])
        if not docs:
            return []
        try:
            qvec = self.embedding.embed([query])[0]
        except Exception:
            return []
        qbg = _bigrams(query)
        alpha = self.semantic_weight
        # 一次建索引 + 单次矩阵点积算完全部语义相似度（query 向量只归一化一次）
        idx = self._retrieval_index(did, docs)
        mat = idx["mat"]
        q = np.asarray(qvec, dtype=np.float32)
        nq = float(np.linalg.norm(q))
        qn = (q / nq) if nq else np.zeros_like(q)
        if mat.shape[1] != len(q):
            # 维度不符（如 embedding 后端切换中）⇒ 语义分全部记 0，与原 _sim 行为一致
            sims = np.zeros(len(docs), dtype=np.float32)
        else:
            sims = mat.dot(qn)
        scored = []
        for i, d in enumerate(docs):
            sim = float(sims[i])
            kw = self._keyword_score(query, d, qbg, idx["tbg"][i], idx["xbg"][i])
            if sim <= 1e-9 and kw <= 0.0:
                continue
            fused = alpha * sim + (1.0 - alpha) * kw
            scored.append((fused, sim, kw, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for fused, sim, kw, d in scored[:top_k]:
            cd = _clean_doc(d)
            cd["_sim"] = round(sim, 4)
            cd["_kw"] = round(kw, 4)
            out.append(cd)
        return out

    def retrieve(self, kb_id, query, top_k=None):
        """兼容接口：仅返回文本列表（语义召回，供 render 等使用）。"""
        return [d["text"] for d in self.hybrid_retrieve(kb_id, query, top_k)]

    # ---- 城市感知 rerank（检索质量升级） ----
    # ⚠️ 2026-09-26 删除死代码 `_city_vocab`：全仓 grep 仅剩定义、无任何调用点
    #    （城市感知 rerank 现用 _detect_query_city 的静态词表，不扫库）。
    def _detect_query_city(self, query):
        """从 query 抽取用户意图城市（标准地级市集合，排除省级如「湖北」）。"""
        q = query or ""
        tokens = list(_HUBEI_PREFECTURES) + list(_CITY_ALIASES.keys())
        found = set()
        for tok in tokens:
            if tok in q:
                canon = _CITY_ALIASES.get(tok, tok)
                if canon not in _PROVINCE_LEVEL:
                    found.add(canon)
        return found

    def _is_cross_region_query(self, query):
        """C5 跨域过滤：query 是否显式提到某主要「非湖北」城市/地标（见 _CROSS_REGION_PLACES）。

        命中即视为跨域意图。配合 query() 的中低分段路由门：跨域 query 若仅靠语义撞词拿到
        中等融合分（如「上海迪士尼」→ 武当山「游玩攻略」），不应被推入 RAG 用不相关湖北文档作答，
        改走兜底（通用 LLM 答通识）更安全。
        """
        q = query or ""
        return any(p in q for p in _CROSS_REGION_PLACES)

    def rerank(self, kb_id, query, candidates, top_k=None):
        """对混合检索候选重排：同城加权、跨具体城市降权、专名精确命中加权。

        返回重排后的 top_k（默认 self.top_k）。不改动阈值判定（阈值仍用原始融合分）。
        """
        if not candidates:
            return candidates
        top_k = top_k or self.top_k
        qcities = self._detect_query_city(query)
        qnorm = re.sub(r"\s+", "", query or "")
        scored = []
        for d in candidates:
            fused = self.semantic_weight * d.get("_sim", 0) + (1.0 - self.semantic_weight) * d.get("_kw", 0)
            final = fused
            doc_city = (d.get("metadata", {}) or {}).get("city", "")
            if qcities:
                if doc_city in qcities:
                    final += self.rerank_city_boost
                elif doc_city and doc_city not in _PROVINCE_LEVEL:
                    final -= self.rerank_city_penalty
            title = d.get("title", "")
            if title and len(title) >= 2 and title in qnorm:
                final += self.rerank_name_boost
            scored.append((final, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:top_k]]

    def retrieve_reranked(self, kb_id, query, top_k=None):
        """取较大候选池（原始融合分排序）后再做城市感知重排，返回重排后的 top_k。"""
        top_k = top_k or self.top_k
        pool_k = max(top_k, self.rerank_candidate_k)
        pool = self.hybrid_retrieve(kb_id, query, top_k=pool_k)
        if not self.rerank_enabled:
            return pool[:top_k]
        return self.rerank(kb_id, query, pool, top_k=top_k)

    # ---- faithfulness 护栏 ----
    def _all_kb_titles(self, kb_id):
        did = _norm_kb_id(kb_id)
        data = self._get_mem(did)
        return [d.get("title", "") for d in data.get("docs", []) if d.get("title")]

    def _faithfulness_check(self, kb_id, answer, ctx_titles):
        """检查回答中提及的 KB 内 POI 名是否都出自本次检索上下文；返回未在上下文出现的 POI 名列表。"""
        if not answer or not self.faith_enabled:
            return []
        titles = self._all_kb_titles(kb_id)
        ctx = set(ctx_titles or [])
        mentioned = {t for t in titles if len(t) >= 2 and t in answer}
        return [t for t in mentioned if t not in ctx]

    def _numeric_faithfulness_check(self, kb_id, answer, source_texts):
        """数值精度护栏：扫描答案中的面积/长度/质量/温度/年份，与检索上下文比对。

        返回警示字符串列表（空=通过）。两类命中：
          1) 答案出现某计量类别但 KB 检索上下文完全没有同单位数值 → 可能编造计量；
          2) 同单位但数值超出容差 → 数字与 KB 记录不符（数字美化/记错）。
        零额外 LLM 成本（纯正则+比对）。
        """
        if not answer or not self.faith_numeric_enabled:
            return []
        ans_nums = _extract_numbers(answer)
        if not ans_nums:
            return []
        src_nums = []
        for t in (source_texts or []):
            src_nums.extend(_extract_numbers(t))
        flags = []
        for a in ans_nums:
            cls = a["cls"]
            same = [s for s in src_nums if s["cls"] == cls]
            if not same:
                # C3：年份/温度为常识或通用上下文（如「始建于223年」「昼夜温差10℃」），
                # 检索上下文未必含同类数字却常被答案顺带提及 → 缺失类不再误报「编造计量」，
                # 消除神农架徒步等随机 caveat。area/len/mass 仍保留缺失类警示（硬计量，编造风险高）。
                if cls in ("year", "temp"):
                    continue
                flags.append("知识库未检索到相关「%s」数据（答：%s）" % (_NUM_CLASS_CN[cls], a["raw"]))
                continue
            closest = min(same, key=lambda s: abs(s["value"] - a["value"]))
            if abs(closest["value"] - a["value"]) > _num_tol(cls, a["value"], closest["value"]):
                flags.append("数字「%s」与知识库记录（%s）不符" % (a["raw"], closest["raw"]))
        return flags

    def _entity_attribution_check(self, kb_id, answer, ctx_titles=None, title_numbers=None):
        """实体级归属校验：防止把 A 条目的数字安到 B 条目（张冠李戴）。

        思路与三处改进见模块级纯函数 _entity_attribution_core（零 LLM / 可单测）：
          (a) 同源多条目归一（避免荆州古城类误报）；
          (b) 复合行政区专名不误抽（东湖高新区→跳过东湖）；
          (c) 就近归属优先取数字前面 POI。
        ctx_titles（C3 新增）：本次实际喂给 LLM 的检索条目标题；用于计算 retrieved_canon，
        把「张冠李戴」收窄到真正归属 POI 也被检索到的情形，消除未检索条目的巧合同数误报。
        """
        if not answer or not self.faith_entity_enabled:
            return []
        if title_numbers is None:
            did = _norm_kb_id(kb_id)
            data = self._get_mem(did)
            # ⚠️ 2026-09-26 性能优化：原本每次调用都对全库 302 条逐篇跑 _extract_numbers()
            #    （实测护栏 ~6.8ms，是整套非 LLM 开销的主导项）。改为按库内容签名缓存，
            #     库不变则直接复用（签名逻辑见 _docs_signature）。
            title_numbers = self._title_numbers(did, data.get("docs", []))
        # C3：仅当确实给了检索上下文才收窄；空上下文保持旧行为（不误杀全部）
        retrieved_canon = (
            {_canon_spot(t) for t in ctx_titles} if ctx_titles else None
        )
        return _entity_attribution_core(answer, title_numbers, retrieved_canon=retrieved_canon)

    def _title_numbers(self, kb_id, docs):
        """标题 → 该条目正文中的数字列表，按库内容签名缓存（库不变则复用）。"""
        sig = self._docs_signature(docs)
        ent = self._title_num_cache.get(kb_id)
        if ent is not None and ent.get("sig") == sig:
            return ent["tn"]
        title_numbers = {}
        for d in docs:
            t = d.get("title", "")
            if len(t) >= 2:
                title_numbers[t] = _extract_numbers(d.get("text", ""))
        self._title_num_cache[kb_id] = {"sig": sig, "tn": title_numbers}
        return title_numbers

    def _llm_faithfulness_review(self, cfg, question, ctx, answer):
        """语义级事实一致性复核（可选安全网，默认关，见 fcfg.llm_review_enabled）。

        动机：前三道护栏（POI 名对齐 / 数值精度 / 实体级归属）都是正则级，抓不到
        「语义层面的编造/矛盾」——例如把上下文里没有的开放时间、票价、里程、路线细节写进答案。
        LLM 复核在合成后用一次低成本审查把这类幻觉拦下来。

        安全设计（默认关，零回归前提下才值得开）：
          - 默认关闭：不调用则不产生任何额外 LLM 成本、不改变任何答案；
          - 只追加「仅供参考」软警示（faithfulness=caution），绝不改写答案；
          - 调用失败/超时只记日志、返回空，降级为「不复核」，不影响正常回答；
          - 在正则护栏之后跑，仅作补充层（不改写正则护栏已有的判定）。
        """
        if not self.faith_llm_review_enabled:
            return []
        if not answer or not ctx:
            return []
        llm = self._llm_instance(cfg, self.query_llm_block) if self.query_enabled else None
        if llm is None:
            return []
        sys_prompt = (
            "你是严谨的文旅问答事实一致性审查员。下面给出「知识库上下文」「用户问题」「助手回答」。"
            "请逐条检查：回答中出现的具体事实陈述（如开放时间、票价、里程、建成年代、景点地位/称号、"
            "路线细节等）是否有与知识库上下文相矛盾、或知识库上下文根本未提及却被当作确定事实陈述的内容。"
            "只审查可由上下文验证的事实，不审查主观描述与常识。\n"
            "输出规则：若没有问题，只输出一行 OK；若有问题，每行输出一句「问题：<具体哪里与上下文不符或属无据陈述>」，"
            "最多列 3 条。不要输出其他解释文字。"
        )
        user_prompt = (
            f"<知识库上下文>\n{ctx}\n</知识库上下文>\n\n"
            f"用户问题：{question}\n\n助手回答：{answer}\n\n审查结果："
        )
        try:
            parts = []
            for chunk in llm.response_no_stream(
                system_prompt=sys_prompt, user_prompt=user_prompt
            ):
                parts.append(chunk)
            raw = "".join(parts).strip()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"文旅 LLM 复核失败(降级不复核): {e}")
            return []
        if not raw or raw.strip().upper() == "OK":
            return []
        # 切分：按「问题：」/ 换行 / 行首编号（如「2、」「3.」）切，排除小数点误切（[0-9]+[.、](?!\d)）。
        flags = []
        for seg in re.split(r"问题[:：]|\r?\n|\s*[0-9]+[.、](?!\d)", raw):
            seg = seg.strip().strip("。")
            if seg and seg.upper() != "OK":
                flags.append("LLM复核：" + seg)
        return flags

    def warmup(self, kb_ids=None):
        """启动预热：把「进程级一次性开销」从首个真实请求挪到启动阶段。

        ⚠️ 为什么需要（2026-09-27 容器内实测，302 条 / 512 维）：
             embedding 模型首次加载 352ms + 建检索索引 24ms + title_numbers 冷建 5.5ms
             ⇒ 进程重启后**首个真实请求约 390ms**（其中绝大多数是模型加载），之后才降到 5ms 量级。
           预热即把这三项提前做掉，首个请求直接落在热态。

        ⚠️ 只提前构建缓存，不改变任何检索分数/排序/答案（缓存均以 _docs_signature 为失效键，
           预热与真实查询走同一条构建路径）。

        默认关闭（TourismKB.warmup.enabled=false）：未接文旅流量的进程不应为它常驻模型内存。
        """
        if not self.enabled:
            return {"warmed": [], "ms": 0.0, "model_ms": 0.0}
        ids = list(kb_ids) if kb_ids else list(self.warmup_kb_ids)
        out = {"warmed": [], "ms": 0.0, "model_ms": 0.0, "docs": 0}
        t0 = time.time()
        try:
            self.embedding.embed(["预热"])
            out["model_ms"] = round((time.time() - t0) * 1000, 1)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"文旅 KB 预热 embedding 失败(忽略): {e}")
        for kid in ids:
            try:
                did = _norm_kb_id(kid)
                data = self._ensure_vectors(did, self._get_mem(did))
                docs = data.get("docs", [])
                if not docs:
                    continue
                self._retrieval_index(did, docs)
                self._title_numbers(did, docs)
                out["warmed"].append(did)
                out["docs"] += len(docs)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"文旅 KB 预热 {kid} 失败(忽略): {e}")
        out["ms"] = round((time.time() - t0) * 1000, 1)
        logger.bind(tag=TAG).info(f"文旅 KB 预热完成: {out}")
        return out

    def render(self, kb_id, query):
        """渲染 <tourism_knowledge> 块；未启用/无相关返回空串。"""
        if not self.enabled:
            return ""
        try:
            docs = self.hybrid_retrieve(kb_id, query)
            if not docs:
                return ""
            block = "<tourism_knowledge>\n" + "\n".join(
                f"- 【{d['title']}】{d['text']}" for d in docs
            ) + "\n</tourism_knowledge>"
            return block
        except Exception as e:
            logger.bind(tag=TAG).warning(f"文旅知识渲染失败(降级空): {e}")
            return ""

    # ---- LLM 实例（与 FamilyRAG 同范式） ----
    def _llm_instance(self, config, block):
        if self._test_llm is not None:
            return self._test_llm
        with self._llm_lock:
            inst = self._llm_cache.get(block)
            if inst is None:
                llm_cfg = (config.get("LLM") or {}).get(block)
                if not llm_cfg:
                    logger.bind(tag=TAG).warning(f"文旅 LLM 配置块缺失: {block}")
                    return None
                llm_type = llm_cfg.get("type", "openai")
                try:
                    inst = create_instance(llm_type, llm_cfg)
                    self._llm_cache[block] = inst
                except Exception as e:
                    logger.bind(tag=TAG).error(f"文旅 LLM 实例化失败: {e}")
                    return None
            return inst

    # ---- 问答合成 ----
    def query(self, kb_id, question, config=None, use_llm=None):
        """检索 + 基于 <知识库> 上下文的 LLM 合成回答；检索未命中时降级为 LLM 通用知识即时生成。

        检索质量升级：先取较大候选池（原始融合分），用 best_fused 做兜底/置信分档（与旧行为一致），
        再对候选做城市感知重排得到真正喂给 LLM 的 top_k；答案合成后跑 faithfulness 护栏。
        """
        if not self.enabled or not question:
            return {"answer": "", "sources": [], "retrieved": []}
        cfg = config if isinstance(config, dict) else _global_config()
        # 1) 原始候选池 + 阈值判定（best_fused 与旧实现完全一致，保证兜底/置信分档不变）
        pool = self.hybrid_retrieve(kb_id, question, top_k=max(self.top_k, self.rerank_candidate_k))
        best_fused = (
            max(
                (self.semantic_weight * d.get("_sim", 0) + (1.0 - self.semantic_weight) * d.get("_kw", 0))
                for d in pool
            )
            if pool else 0.0
        )
        if use_llm is not False:
            if (not pool) or (best_fused < self.fallback_threshold):
                return self._query_general(kb_id, question, cfg, related_docs=pool)
            # C5 跨域过滤（城市感知路由门）：若 query 显式提到主要「非湖北」城市/地标，且融合分
            # 处中低段（< high_conf），说明命中只是语义撞词（如「上海迪士尼」→ 武当山「游玩攻略」），
            # 推入 RAG 会用不相关湖北文档产出张冠李戴的错误答案 → 改走兜底（通用 LLM 答通识，更安全）。
            # 高置信（≥ high_conf）仍走 RAG：此时语义确实强相关，跨域词多为对比/举例，兜底反而丢信息。
            if self._is_cross_region_query(question) and best_fused < self.high_conf_threshold:
                return self._query_general(kb_id, question, cfg, related_docs=pool)
        # 2) 城市感知重排，得到喂给 LLM 的 top_k
        docs = self.rerank(kb_id, question, pool, top_k=self.top_k) if self.rerank_enabled else pool[:self.top_k]
        sources = [d["title"] for d in docs]
        ctx = "\n\n".join(
            f"[{i + 1}] 《{d['title']}》（{d.get('type', '')}）\n{d['text']}"
            for i, d in enumerate(docs)
        )
        if use_llm is False:
            answer = "\n".join(f"【{d['title']}】{d['text']}" for d in docs)
            return {"answer": answer, "sources": sources, "retrieved": docs,
                    "confidence": "high", "faithfulness": "grounded"}

        llm = self._llm_instance(cfg, self.query_llm_block) if self.query_enabled else None
        if llm is None:
            answer = "\n".join(f"【{d['title']}】{d['text']}" for d in docs)
            return {"answer": answer, "sources": sources, "retrieved": docs,
                    "confidence": "high", "faithfulness": "grounded"}

        # 高置信 → 标准严格 RAG；中等相关 → 加「仅依据明确支持内容」强约束并标记 medium
        confidence = "high" if best_fused >= self.high_conf_threshold else "medium"
        sys_prompt = (
            "你是湖北文旅问答助手。仅依据下面<知识库>中的内容回答用户问题；"
            "若知识库未涵盖，请明确说明不知道，不要编造；"
            "回答末尾用「来源：xxx」标注引用的景点/条目名称。"
        )
        if confidence == "medium":
            sys_prompt += (
                "注意：以下资料与用户问题的相关度中等，可能仅部分相关。"
                "请仅依据其中明确支持该问题的内容作答；若资料不足以回答，"
                "请坦诚说明信息有限，不要强行拼凑或编造。"
            )
        user_prompt = (
            f"<知识库>\n{ctx}\n</知识库>\n\n用户问题：{question}\n\n回答："
        )
        try:
            parts = []
            for chunk in llm.response_no_stream(
                system_prompt=sys_prompt, user_prompt=user_prompt
            ):
                parts.append(chunk)
            answer = "".join(parts).strip()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"文旅问答 LLM 失败(降级拼接): {e}")
            answer = "\n".join(f"【{d['title']}】{d['text']}" for d in docs)

        # 3) faithfulness 护栏：POI 名对齐 + 数值精度 + 实体级归属，任一命中即追加「仅供参考」警示
        # （防虚构/跨城/数字美化/张冠李戴）。实体归属比「数值不符」更精确，同一数字只保留实体归属说明。
        faithfulness = "grounded"
        caveats = []
        if self.faith_enabled:
            unsupported = self._faithfulness_check(kb_id, answer, sources)
            if unsupported:
                caveats.append("「" + "、".join(unsupported) + "」未直接出自本次检索到的知识库条目")
            if self.faith_numeric_enabled:
                num_flags = self._numeric_faithfulness_check(
                    kb_id, answer, [d.get("text", "") for d in docs]
                )
            else:
                num_flags = []
            if self.faith_entity_enabled:
                ent_flags = self._entity_attribution_check(kb_id, answer, ctx_titles=sources)
            else:
                ent_flags = []
            # 去重：实体归属说明更精确，同一数字的「数值不符」警示让位给实体归属
            num_raws = set()
            for f in num_flags:
                m = re.search(r"数字「([^」]+)」", f)
                if m:
                    num_raws.add(m.group(1))
            caveats = list(num_flags)
            for ef in ent_flags:
                m = re.search(r"数字「([^」]+)」", ef)
                raw = m.group(1) if m else ""
                if raw and raw in num_raws:
                    caveats = [c for c in caveats
                               if not (("数字「%s」" % raw) in c and "不符" in c)]
                caveats.append(ef)
            # 语义级 LLM 复核（默认关）：正则护栏之后跑，仅追加软警示，不改写答案
            if self.faith_llm_review_enabled:
                for lf in self._llm_faithfulness_review(cfg, question, ctx, answer):
                    caveats.append(lf)
            if caveats and self.faith_caveat:
                faithfulness = "caution"
                answer = answer.rstrip("。") + "。（注：" + "；".join(caveats) + "，仅供参考）"
        return {"answer": answer, "sources": sources, "retrieved": docs,
                "confidence": confidence, "faithfulness": faithfulness}

    def _query_general(self, kb_id, question, cfg, related_docs=None):
        """检索未命中/弱命中兜底：半接地生成。

        - 优先把检索到的弱相关文档作为「可能相关的背景」（仅供参考、可能不完全匹配）一并喂给 LLM，
          降低纯自由生成的编造风险；若背景不足或无关，则回退到 LLM 通用知识并坦诚说明。
        - LLM 缺失/异常 → 安全降级「知识库中暂无相关信息」。
        """
        if not self.query_enabled:
            return {"answer": "知识库中暂无相关信息。", "sources": [], "retrieved": [], "confidence": "fallback"}
        llm = self._llm_instance(cfg, self.query_llm_block)
        if llm is None:
            return {"answer": "知识库中暂无相关信息。", "sources": [], "retrieved": [], "confidence": "fallback"}
        sys_prompt = (
            "你是湖北文旅问答助手，回答用户关于湖北文旅的问题"
            "（可涵盖景点、民俗、传说、饮食、历史、地理等）。"
            "若对某一具体细节确实不了解，请坦诚说明，不要编造。"
            "回答末尾用「来源：通用知识整理」标注。"
        )
        # 半接地：弱相关文档作为背景参考，但明确告知可能不完全匹配、勿当确证事实
        bg = ""
        if related_docs:
            bg_docs = "\n\n".join(
                f"[{i + 1}] 《{d.get('title', '')}》（{d.get('type', '')}）\n{d.get('text', '')}"
                for i, d in enumerate(related_docs[:5])
            )
            if bg_docs.strip():
                bg = (
                    "以下是从知识库中检索到的「可能相关的背景资料」（相关性未经确认，可能不完全匹配，"
                    "仅供酌情参考，请勿当作确证事实）：\n"
                    f"<背景资料>\n{bg_docs}\n</背景资料>\n\n"
                )
        user_prompt = f"{bg}用户问题：{question}\n\n回答："
        try:
            parts = []
            for chunk in llm.response_no_stream(
                system_prompt=sys_prompt, user_prompt=user_prompt
            ):
                parts.append(chunk)
            answer = "".join(parts).strip()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"文旅问答通用兜底 LLM 失败(降级): {e}")
            return {"answer": "知识库中暂无相关信息。", "sources": [], "retrieved": [], "confidence": "fallback"}
        return {"answer": answer, "sources": ["通用知识整理"], "retrieved": related_docs or [], "confidence": "fallback"}

    # ---- 入库（LLM 辅助结构化抽取） ----
    def ingest_raw(self, kb_id, raw_text, config=None, base_metadata=None):
        """从原始资料用 LLM 结构化抽取旅游条目并入库。返回新增条数。绝不抛异常。"""
        if not self.enabled or not self.ingest_enabled or not raw_text:
            return 0
        cfg = config if isinstance(config, dict) else _global_config()
        llm = self._llm_instance(cfg, self.ingest_llm_block)
        if llm is None:
            return 0
        prompt = _INGEST_PROMPT.format(raw=raw_text)
        try:
            parts = []
            for chunk in llm.response_no_stream(
                system_prompt="你是文旅信息抽取器，只输出 JSON 数组。",
                user_prompt=prompt,
            ):
                parts.append(chunk)
            raw = "".join(parts)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"抽取 LLM 调用失败(降级跳过): {e}")
            return 0
        items = self._parse_docs(raw)
        if not items:
            return 0
        n = 0
        for it in items:
            meta = dict(base_metadata or {})
            meta.update(it.get("metadata") or {})
            if self.add_doc(kb_id, it.get("type"), it.get("title"), it.get("text"), meta):
                n += 1
        return n

    def ingest_structured(self, kb_id, items):
        """直接接收已结构化条目列表（demo / 批处理用）。返回新增条数。"""
        if not self.enabled or not items:
            return 0
        n = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            meta = dict(it.get("metadata") or {})
            if self.add_doc(kb_id, it.get("type"), it.get("title"), it.get("text"), meta):
                n += 1
        return n

    @staticmethod
    def _parse_docs(text):
        if not text:
            return []
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return []
        try:
            arr = json.loads(m.group(0))
        except Exception:
            return []
        if not isinstance(arr, list):
            return []
        out = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            text = (item.get("text") or "").strip()
            if len(title) < 1 or len(text) < 4:
                continue
            t = item.get("type") or "other"
            if t not in _KB_TYPES:
                t = "other"
            meta = item.get("metadata") or {}
            if isinstance(meta, dict):
                tags = meta.get("tags") or []
                if isinstance(tags, str):
                    tags = [x.strip() for x in tags.split(",") if x.strip()]
                meta["tags"] = list(tags)[:10]
            else:
                meta = {}
            out.append({"type": t, "title": title, "text": text, "metadata": meta})
        return out


_instance = None
_instance_lock = threading.Lock()


def _global_config():
    """从全局配置单例取配置（懒导入，避免顶层循环依赖）。"""
    try:
        from fuguoai_core.utils.config_holder import get_config
        return get_config() or {}
    except Exception:
        return {}


def get_tourism_kb(config=None):
    """进程级单例；支持运行时热更新 enabled 开关（与 get_family_rag 同范式）。"""
    global _instance
    with _instance_lock:
        if _instance is None:
            cfg = config if isinstance(config, dict) else _global_config()
            _instance = TourismKB(cfg)
        elif config is not None:
            tk_cfg = (config.get("TourismKB", {}) or {}) if isinstance(config, dict) else {}
            _instance.enabled = bool(tk_cfg.get("enabled", False))
            q = (tk_cfg.get("query") or {}) or {}
            _instance.query_enabled = bool(q.get("enabled", True))
            ic = (tk_cfg.get("ingest") or {}) or {}
            _instance.ingest_enabled = bool(ic.get("enabled", True))
        return _instance
