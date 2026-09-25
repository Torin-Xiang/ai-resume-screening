"""合规红线（方案 §4.5，不可妥协）。

性别、年龄、婚育状况、籍贯、民族、健康状况、政治面貌 **不得进入评分维度**。
模型容易从毕业年份反推年龄、从姓名推断性别，因此：
1. rubric 加载 / 生成时，对维度名称、definition、正反例做关键词拦截；
2. LLM 输出（判定理由）做同样的拦截，命中即标记并要求剔除。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 敏感属性 → 触发词（覆盖中英文与常见隐晦表达）
# 注意：英文词必须加词边界 \b，否则 "Agent"/"average" 会被 "age" 误判为年龄维度。
SENSITIVE_PATTERNS: dict[str, list[str]] = {
    "性别": [r"性别", r"男女", r"男性", r"女性", r"男生", r"女生", r"\bgender\b", r"\bmale\b", r"\bfemale\b"],
    "年龄": [
        r"年龄",
        r"\d+\s*岁以下",
        r"\d+\s*岁以上",
        r"年纪",
        r"出生",
        r"生日",
        r"\bage\b",
        r"年轻化",
        r"几几年的",
    ],
    "婚育": [r"婚育", r"已婚", r"未婚", r"生育", r"怀孕", r"育儿", r"子女", r"\bmarriage\b", r"\bpregnan\w*\b"],
    "籍贯": [r"籍贯", r"户籍", r"户口", r"老家", r"哪里人", r"本地人", r"\bhukou\b"],
    "民族": [r"民族", r"汉族", r"少数民族", r"种族", r"\bethnic\w*\b"],
    "健康": [r"健康", r"病史", r"残疾", r"体检", r"疾病", r"\bhealth\b", r"\bdisability\b"],
    "政治面貌": [r"政治面貌", r"党员", r"团员", r"民主党派", r"政治立场"],
}

_COMPILED: dict[str, re.Pattern[str]] = {
    cat: re.compile("|".join(f"(?:{w})" for w in words), re.IGNORECASE)
    for cat, words in SENSITIVE_PATTERNS.items()
}


class ComplianceViolation(ValueError):
    """评分维度或模型输出触碰合规红线。"""


@dataclass(frozen=True)
class SensitiveHit:
    category: str
    keyword: str
    field: str


def scan_text(text: str) -> list[tuple[str, str]]:
    """返回 [(敏感类别, 命中词)]。"""
    hits: list[tuple[str, str]] = []
    for cat, pat in _COMPILED.items():
        m = pat.search(text or "")
        if m:
            hits.append((cat, m.group(0)))
    return hits


def check_text(text: str, *, field: str = "text") -> list[SensitiveHit]:
    return [SensitiveHit(cat, kw, field) for cat, kw in scan_text(text)]


def ensure_clean(text: str, *, field: str = "text") -> None:
    hits = check_text(text, field=field)
    if hits:
        detail = "、".join(f"{h.field} 命中[{h.category}]“{h.keyword}”" for h in hits)
        raise ComplianceViolation(
            f"合规红线：检测到受保护属性进入评分相关内容（{detail}）。"
            "依据《就业促进法》与方案 §4.5，性别/年龄/婚育/籍贯/民族/健康/政治面貌不得参与评分。"
        )


PROMPT_COMPLIANCE_CLAUSE = (
    "合规红线（必须遵守）：禁止使用或推断候选人的性别、年龄、出生年份、婚育状况、"
    "籍贯/户籍、民族、健康状况、政治面貌等受保护属性，也不得从毕业年份、入学时间反推年龄，"
    "或从姓名推断性别。违反此条的输出将被系统拒绝。"
)
