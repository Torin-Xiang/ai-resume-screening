"""证据校验（方案 §4.3：禁止编造）。

落地方式：模型给出的每条 quote 都必须能在简历原文中找到（去空白后做子串匹配）。
找不到 → 该判定不可信 → 降级为「未提及/无法判定」，绝不作为淘汰依据。
"""

from __future__ import annotations

import re

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[，。、；：,.;:（）()【】\[\]「」“”\"'·\-—_/\\|]+")


def normalize(text: str) -> str:
    """归一化：去掉空白与常见标点，便于容忍 PDF 抽取带来的换行/标点差异。"""
    t = _WS.sub("", text or "")
    return _PUNCT.sub("", t)


def quote_in_text(quote: str, raw_text: str) -> bool:
    """quote 是否逐字出现在原文中（忽略空白与标点差异）。"""
    if not quote or not raw_text:
        return False
    return normalize(quote) in normalize(raw_text)


def find_quote_page(quote: str, raw_text: str, default: int | None = None) -> int | None:
    """尽力定位 quote 所在页码（用于导出时给出处）。原文不分页时返回 default。"""
    return default
