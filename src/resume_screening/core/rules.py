"""内置硬门槛规则（方案 §4.2：硬门槛用规则，不用 LLM）。

规则的价值：成本 0、准确率 100%、完全可解释。LLM 算"工作年限""是否本科"这类会算错，且更贵。
代价：新增规则要在这里注册（评分卡里用 rule id 引用 + params 传参）。

每条规则返回 (judgement, reason, evidence)：
- 字段未提及 → NOT_MENTIONED（**不得据此淘汰**，下游标"待确认"）
- 字段已提取但不达标 → NOT_SATISFIED（可一票否决）
"""

from __future__ import annotations

import re
from datetime import date
from typing import Callable, Optional

from .models import Evidence, ExtractStatus, Judgement, ResumeDocument

# ---------------- 基础工具 ----------------
DEGREE_LEVELS: dict[str, int] = {
    "博士": 4,
    "硕士": 3,
    "研究生": 3,
    "本科": 2,
    "学士": 2,
    "大专": 1,
    "专科": 1,
}

_MONTH_RE = re.compile(r"(20\d{2})\s*[.\-/年]\s*(\d{1,2})")
_YEAR_RE = re.compile(r"(20\d{2})")
_CURRENT = ("至今", "现在", "目前", "present", "now", "current")


def degree_level(text: str | None) -> int | None:
    """从学历文本中取最高等级。"""
    if not text:
        return None
    level: int | None = None
    for key, lv in DEGREE_LEVELS.items():
        if key in text:
            level = lv if level is None else max(level, lv)
    return level


def parse_ym(text: str | None) -> tuple[int, int] | None:
    """解析 '2022.07' / '2022-07' / '2022年7月' / '2022'。"""
    if not text:
        return None
    m = _MONTH_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _YEAR_RE.search(text)
    if m:
        return int(m.group(1)), 1
    return None


def is_current(text: str | None) -> bool:
    return bool(text) and any(k in text for k in _CURRENT)


def months_between(start: tuple[int, int], end: tuple[int, int]) -> int:
    return (end[0] - start[0]) * 12 + (end[1] - start[1])


def average_tenure_months(doc: ResumeDocument) -> Optional[float]:
    """平均在职月数（用于跳槽频次）。无经历或无法解析 → None（→ 未提及）。"""
    today = (date.today().year, date.today().month)
    spans: list[int] = []
    for exp in doc.fields.work_experiences:
        s = parse_ym(exp.start)
        if not s:
            continue
        e = today if is_current(exp.end) else parse_ym(exp.end)
        if not e:
            continue
        months = months_between(s, e)
        if months > 0:
            spans.append(months)
    if not spans:
        return None
    return sum(spans) / len(spans)


# ---------------- 规则实现 ----------------
RuleFn = Callable[[ResumeDocument, dict], tuple[Judgement, str, Evidence | None]]


def rule_min_degree(doc: ResumeDocument, params: dict) -> tuple[Judgement, str, Evidence | None]:
    """最高学历不低于 min_level（默认 本科）。"""
    min_level_text = str(params.get("min_level") or "本科")
    need = degree_level(min_level_text) or 2

    texts: list[str] = []
    evidence: Evidence | None = None
    if doc.fields.education.is_known:
        texts.append(str(doc.fields.education.value))
        evidence = doc.fields.education.evidence
    for rec in doc.fields.education_records:
        if rec.degree:
            texts.append(rec.degree)
            evidence = evidence or rec.evidence
    if not texts:
        return Judgement.NOT_MENTIONED, "简历未提及学历，无法判定（不得据此淘汰）", None

    got = max((degree_level(t) or 0) for t in texts)
    if got == 0:
        return Judgement.NOT_MENTIONED, "学历文本无法识别层次，待人工确认", None
    ok = got >= need
    return (
        Judgement.SATISFIED if ok else Judgement.NOT_SATISFIED,
        f"学历等级 {got} {'≥' if ok else '<'} 要求 {need}（要求：{min_level_text}）",
        evidence,
    )


def rule_min_years(doc: ResumeDocument, params: dict) -> tuple[Judgement, str, Evidence | None]:
    """工作年限不低于 min_years。"""
    need = float(params.get("min_years") or 0)
    if not doc.fields.years_of_experience.is_known:
        return Judgement.NOT_MENTIONED, "简历未提及工作年限，无法判定（不得据此淘汰）", None
    got = float(doc.fields.years_of_experience.value or 0)
    ok = got >= need
    return (
        Judgement.SATISFIED if ok else Judgement.NOT_SATISFIED,
        f"工作年限 {got:g} 年 {'≥' if ok else '<'} 要求 {need:g} 年",
        doc.fields.years_of_experience.evidence,
    )


def rule_location(doc: ResumeDocument, params: dict) -> tuple[Judgement, str, Evidence | None]:
    """现居城市须在 cities 列表中。"""
    cities = [str(c) for c in (params.get("cities") or [])]
    if not cities:
        return Judgement.NOT_MENTIONED, "评分卡未配置 cities，跳过该规则", None
    if not doc.fields.location.is_known:
        return Judgement.NOT_MENTIONED, "简历未提及现居城市，无法判定（不得据此淘汰）", None
    loc = str(doc.fields.location.value)
    ok = any(c in loc for c in cities)
    return (
        Judgement.SATISFIED if ok else Judgement.NOT_SATISFIED,
        f"现居城市「{loc}」{'在' if ok else '不在'}要求范围 {cities}",
        doc.fields.location.evidence,
    )


def rule_major(doc: ResumeDocument, params: dict) -> tuple[Judgement, str, Evidence | None]:
    """专业须命中 keywords 之一。"""
    keywords = [str(k) for k in (params.get("keywords") or [])]
    if not keywords:
        return Judgement.NOT_MENTIONED, "评分卡未配置专业 keywords，跳过该规则", None
    if not doc.fields.major.is_known:
        return Judgement.NOT_MENTIONED, "简历未提及专业，无法判定（不得据此淘汰）", None
    major = str(doc.fields.major.value)
    ok = any(k in major for k in keywords)
    return (
        Judgement.SATISFIED if ok else Judgement.NOT_SATISFIED,
        f"专业「{major}」{'命中' if ok else '未命中'}要求关键词 {keywords}",
        doc.fields.major.evidence,
    )


def rule_job_hop(doc: ResumeDocument, params: dict) -> tuple[Judgement, str, Evidence | None]:
    """平均在职时长不得低于 min_avg_months（跳槽过于频繁）。"""
    need = float(params.get("min_avg_months") or 12)
    avg = average_tenure_months(doc)
    if avg is None:
        return Judgement.NOT_MENTIONED, "工作经历时间无法解析，无法判定（不得据此淘汰）", None
    ok = avg >= need
    return (
        Judgement.SATISFIED if ok else Judgement.NOT_SATISFIED,
        f"平均在职 {avg:.1f} 个月 {'≥' if ok else '<'} 要求 {need:g} 个月",
        None,
    )


RULES: dict[str, RuleFn] = {
    "min_degree": rule_min_degree,
    "min_years": rule_min_years,
    "location": rule_location,
    "major": rule_major,
    "job_hop": rule_job_hop,
}


def run_rule(rule_id: str, doc: ResumeDocument, params: dict) -> tuple[Judgement, str, Evidence | None]:
    fn = RULES.get(rule_id)
    if fn is None:
        return Judgement.UNKNOWN, f"未注册的规则 id：{rule_id}", None
    judgement, reason, evidence = fn(doc, params)
    # 兜底：规则判定为"满足/不满足"但拿不到原文引用时，降级为无法判定，
    # 保证"每条结论必须带原文引用"（方案 §4.3）。
    if judgement in (Judgement.SATISFIED, Judgement.NOT_SATISFIED) and evidence is None:
        if rule_id != "job_hop":  # job_hop 由多段经历计算，证据在字段本身
            return Judgement.UNKNOWN, f"{reason}（缺少原文引用，降级为待确认）", None
    return judgement, reason, evidence
