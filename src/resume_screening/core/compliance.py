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
# 中国的省级行政区（含简称）与常见地级市。
# 放在这里是因为"不要河南的""只要北京人"这类说法里没有"省/市"字样，
# 靠后缀正则抓不到，只能靠地名本身来认。这是**数据**，不是逻辑——
# 名字变了改这一行即可，判定规则不用动。
REGION_NAMES: tuple[str, ...] = (
    # 省级行政区
    "北京", "上海", "天津", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "内蒙古", "广西", "西藏", "宁夏", "新疆", "台湾", "香港", "澳门",
    # 常见地级市（招聘里高频出现，容易成为地域筛选的落点）
    "石家庄", "太原", "沈阳", "长春", "哈尔滨", "南京", "杭州", "合肥", "福州",
    "南昌", "济南", "郑州", "武汉", "长沙", "广州", "深圳", "成都", "贵阳",
    "昆明", "西安", "兰州", "西宁", "苏州", "无锡", "宁波", "青岛", "大连",
    "厦门", "东莞", "佛山", "珠海", "温州", "常州",
)

_REGION_ALT = "|".join(REGION_NAMES)

# 歧视性动词在前："不要河南的""只要北京人""限本市户籍"
_REGION_VERB_CLAUSE = (
    r"(不要|排除|排除掉|筛掉|剔除|不要招|只要|仅限|仅招|只招|限|优先)"
    r"\s*(?:"
    rf"{_REGION_ALT}"
    r"|[\u4e00-\u9fff]{1,4}(?:省|市|县)"
    r")(?:的|人|籍|户籍|户口|本地|候选人)"
)

# 地名在前、歧视性动词在后："江苏人优先"
_REGION_PRIORITY_CLAUSE = rf"(?:{_REGION_ALT})(?:人|籍|户籍|户口)(?:优先|优先考虑|加分)"

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
    "籍贯": [
        r"籍贯",
        r"户籍",
        r"户口",
        r"老家",
        r"哪里人",
        r"本地人",
        r"\bhukou\b",
        # 迂回表达：不写"籍贯"，改成按地域排除。这是地域歧视最常见的说法，
        # 而上面的词一个都不会命中——"不要河南的"里没有任何敏感词。
        #
        # 判定必须同时具备"歧视性动词 + 地名 + 人的指代"，三条缺一不可：
        #   · 只匹配地名 → "工作地点北京"这类合法要求会被误伤
        #   · 只匹配动词 → "不要没有经验的人"会被误伤
        # 宁可略微偏严（HR 换个说法即可，提示里也告诉了怎么换），
        # 也不能漏——合规是红线，而误伤的代价是可恢复的。
        _REGION_VERB_CLAUSE,
        _REGION_PRIORITY_CLAUSE,
        r"(北方|南方)人",
        r"老乡",
    ],
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
