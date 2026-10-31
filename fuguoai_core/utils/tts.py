"""复果 TTS 工具集（clean-room 重写）。

对外提供三件能力：
1. ``create_instance`` —— 按配置里的 type 动态加载并实例化 TTS provider；
2. ``MarkdownCleaner.clean_markdown`` —— 把 LLM 回包里的 Markdown 清洗成
   适合朗读的纯文本（代码块、标题、表格、公式等均做口语化转写）；
3. ``convert_percentage_to_range`` —— 把 -100~100 的百分比线性映射到目标区间。

实现完全独立编写：清洗过程以模块级**有序步骤表**驱动，表格转写与区间映射
各自抽为独立函数；仅在与调用方对接的公开名（``create_instance`` /
``MarkdownCleaner.clean_markdown`` / ``convert_percentage_to_range``）上保持一致。
"""

import os
import re
import sys
import importlib

from fuguoai_config.logger import setup_logging
from fuguoai_core.utils.textUtils import check_emoji

logger = setup_logging()

# 朗读时可直接略过的标点（中英文成对收录）
_SKIPPABLE_PUNCT = frozenset("，,。.！!“”\"：:-－、[]【】~")

# TTS provider 源码目录（相对工程根）与模块包名
_PROVIDER_PARTS = ("fuguoai_core", "providers", "tts")
_PROVIDER_PKG = "fuguoai_core.providers.tts"


def create_instance(class_name, *args, **kwargs):
    """按配置里的 type 加载对应 TTS provider 并实例化。"""
    if not os.path.exists(os.path.join(*_PROVIDER_PARTS, f"{class_name}.py")):
        raise ValueError(f"不支持的TTS类型: {class_name}，请检查该配置的type是否设置正确")

    module_name = f"{_PROVIDER_PKG}.{class_name}"
    module = sys.modules.get(module_name)
    if module is None:
        module = importlib.import_module(module_name)
        sys.modules[module_name] = module
    return module.TTSProvider(*args, **kwargs)


# ---- Markdown → 朗读文本 ----

# 公式字符：用于判断 $...$ 里是否真是数学公式而非货币金额
_FORMULA_CHARS = re.compile(r"[a-zA-Z\\^_{}\+\-\(\)\[\]=]")
# 表格分隔行（`---` / `:--:` 之类），转写正文时需跳过
_TABLE_SEPARATOR = re.compile(r"^[|]\s*[-:]+\s*([|]\s*[-:]+\s*)+[|]?$")


def _looks_like_formula(inner):
    return _FORMULA_CHARS.search(inner) is not None


def _unwrap_inline_dollar(match):
    """`$...$`：内部像公式则去掉两侧 `$`，否则（货币/金额）原样保留。"""
    inner = match.group(1)
    return inner if _looks_like_formula(inner) else match.group(0)


def _row_cells(line):
    """按 `|` 切一行表格，返回去空白后的非空单元格。"""
    return [cell.strip() for cell in line.split("|") if cell.strip()]


def _speak_table(match):
    """把整段表格转写成逐行口语描述；空表返回空串。"""
    rows = []
    for raw_line in match.group("block").strip("\n").split("\n"):
        line = raw_line.strip()
        if _TABLE_SEPARATOR.match(line):
            continue
        cells = _row_cells(line)
        if cells:
            rows.append(cells)

    if not rows:
        return ""

    header, body = rows[0], rows[1:]
    if not body:
        return "单行表格：" + ", ".join(header) + "\n"

    spoken = ["表头是：" + ", ".join(header)]
    for index, row in enumerate(body, start=1):
        cells = [
            f"{header[pos]} = {value}" if pos < len(header) else value
            for pos, value in enumerate(row)
        ]
        spoken.append(f"第 {index} 行：" + ", ".join(cells))
    return "\n".join(spoken) + "\n"


# 清洗步骤（顺序影响结果，不可调整）：(编译后的模式, 替换串或回调)
_CLEAN_STEPS = (
    (re.compile(r"```.*?```", re.DOTALL), ""),
    (re.compile(r"^[#]{1,}\s*", re.MULTILINE), ""),
    (re.compile(r"([*][*]|__)(.*?)\1"), r"\2"),
    (re.compile(r"([*]|[_])(?=\S)(.*?)(?<=\S)\1"), r"\2"),
    (re.compile(r"!\[[^\n]*?\]\([^\n]*?\)"), ""),
    (re.compile(r"\[([^\n]*?)\]\([^\n]*?\)"), r"\1"),
    (re.compile(r"^\s*[>]{1,}\s*", re.MULTILINE), ""),
    (
        re.compile(r"(?P<block>(?:^[^\n]*[|][^\n]*\n)+)", re.MULTILINE),
        _speak_table,
    ),
    (re.compile(r"^\s*[-*+]\s*", re.MULTILINE), "- "),
    (re.compile(r"[$][$].*?[$][$]", re.DOTALL), ""),
    (
        re.compile(r"(?<![A-Za-z0-9])[$]([^\n$]+)[$](?![A-Za-z0-9])"),
        _unwrap_inline_dollar,
    ),
    (re.compile(r"[\n]{2,}"), "\n"),
)


def _is_western(text):
    """纯 ASCII / 空白 / 可读标点时返回 True（西文朗读依赖原文空格分词）。"""
    return all(
        char.isascii() or char.isspace() or char in _SKIPPABLE_PUNCT
        for char in text
    )


def _clean_markdown(text):
    """按 `_CLEAN_STEPS` 顺序清洗，再剥 emoji，并按语种决定是否压缩空白。"""
    for pattern, replacement in _CLEAN_STEPS:
        text = pattern.sub(replacement, text)

    text = check_emoji(text)

    if text and _is_western(text):
        return text
    return text.strip()


class MarkdownCleaner:
    """Markdown → 朗读文本清洗器；对外只暴露 `clean_markdown` 一个入口。"""

    @staticmethod
    def clean_markdown(text: str) -> str:
        """把 Markdown 文本清洗成适合 TTS 朗读的纯文本。"""
        return _clean_markdown(text)


def convert_percentage_to_range(percentage, min_val, max_val, base_val=None):
    """把 -100~100 的百分比线性映射到 [min_val, max_val]（缺省基准为区间中点）。"""
    pct = float(percentage)
    low = float(min_val)
    high = float(max_val)
    base = float(base_val) if base_val is not None else (low + high) / 2

    # 负向朝下界走、正向朝上界走，均按 pct/100 的比例插值
    span = (base - low) if pct < 0 else (high - base)
    return max(low, min(high, base + span * (pct / 100)))
