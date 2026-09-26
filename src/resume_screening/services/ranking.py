"""排名与分组（产品方案 §6.3）。

核心约定：**三分组，而不是一条纯排序**。
「待确认」（简历未提及）必须与「不推荐」（明确不满足）分开——若合并成一条线，
简历里没写到的人会被当成淘汰，这是最危险的误伤。

本模块是**纯函数**：输入已算好的结论，输出分组与排序，不碰数据库、不调模型。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..core.models import CandidateResult, DimensionResult, Judgement, ScreeningVerdict

GROUP_RECOMMEND = "recommend"
GROUP_NEEDS_REVIEW = "needs_review"
GROUP_NOT_RECOMMEND = "not_recommend"

GROUP_LABELS: dict[str, str] = {
    GROUP_RECOMMEND: "推荐",
    GROUP_NEEDS_REVIEW: "待确认",
    GROUP_NOT_RECOMMEND: "不推荐",
}

# 展示顺序：推荐 → 待确认 → 不推荐
GROUP_ORDER: dict[str, int] = {
    GROUP_RECOMMEND: 0,
    GROUP_NEEDS_REVIEW: 1,
    GROUP_NOT_RECOMMEND: 2,
}

_VERDICT_TO_GROUP: dict[ScreeningVerdict, str] = {
    ScreeningVerdict.PASS: GROUP_RECOMMEND,
    ScreeningVerdict.NEEDS_REVIEW: GROUP_NEEDS_REVIEW,
    ScreeningVerdict.REJECT: GROUP_NOT_RECOMMEND,
}

REASON_MAX_CHARS = 30
"""一句话理由的长度上限（产品方案 §6.2）。"""


def group_of(verdict: ScreeningVerdict | str) -> str:
    """判定 → 分组。"""
    v = ScreeningVerdict(verdict) if isinstance(verdict, str) else verdict
    return _VERDICT_TO_GROUP[v]


def _shorten(text: str, limit: int = REASON_MAX_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _first_quote(dim: DimensionResult) -> str:
    return dim.evidence[0].quote if dim.evidence else ""


def one_line_reason(result: CandidateResult) -> str:
    """从已算好的结论派生**一句话理由**（≤30 字，指向具体标准）。

    严格约束（产品方案 §6.2）：必须指向具体标准，禁止"综合素质较好"这类空话。
    这里的实现原则是——**不产生新判断，只从既有判定里挑最该说的那一条**：

    - 不推荐：挑一条明确不满足的硬门槛（这是它被判不推荐的原因）
    - 待确认：优先说明解析问题，否则指出是哪一项没写到
    - 推荐：说明必须项全过，并带上最亮的一条加分项

    真正的原文证据在 `dimensions[].evidence` 里，展开可见；本函数只负责摘要。
    """
    dims = result.dimensions

    if result.verdict is ScreeningVerdict.REJECT:
        blocked = next((d for d in dims if d.blocks), None)
        if blocked is not None:
            quote = _shorten(_first_quote(blocked), 14)
            tail = f"：{quote}" if quote else ""
            return _shorten(f"「{blocked.name}」不满足{tail}")
        return _shorten("存在明确不满足的必须项")

    if result.verdict is ScreeningVerdict.NEEDS_REVIEW:
        if not result.parse_status.value == "ok":
            return _shorten("简历解析质量存疑，需人工确认")
        missing = next(
            (d for d in dims if d.kind == "hard_filter" and d.judgement is Judgement.NOT_MENTIONED),
            None,
        )
        if missing is not None:
            return _shorten(f"「{missing.name}」简历未提及，待确认")
        unclear = next(
            (
                d
                for d in dims
                if d.kind == "hard_filter" and d.judgement in (Judgement.UNKNOWN,)
            ),
            None,
        )
        if unclear is not None:
            return _shorten(f"「{unclear.name}」无法判定，待确认")
        return _shorten("存在无法确认的必须项")

    # PASS：必须项全过，挑一条最亮的加分项
    best = max(
        (d for d in dims if d.kind == "bonus" and d.judgement is Judgement.SATISFIED),
        key=lambda d: d.score,
        default=None,
    )
    if best is not None:
        return _shorten(f"必须项均满足；命中「{best.name}」")
    return _shorten("必须项均满足")


def reason_of(result: CandidateResult) -> str:
    """对外统一入口：结果里已带一句话理由就直接用，否则派生。"""
    inline = getattr(result, "reason_line", "")
    return inline or one_line_reason(result)


def sort_results(results: Iterable[CandidateResult]) -> list[CandidateResult]:
    """分组顺序 → 分数降序 → resume_id 兜底（保证结果稳定可复现）。"""
    return sorted(
        results,
        key=lambda r: (
            GROUP_ORDER[group_of(r.verdict)],
            -float(r.score),
            r.resume_id,
        ),
    )


def build_ranking(
    results: Sequence[CandidateResult], *, reasons: dict[str, str] | None = None
) -> dict[str, Any]:
    """构造前端直接可用的三分组结构。

    `reasons`：可选的 resume_id → 一句话理由映射（已落在库里的那份），
    传入则用它覆盖实时派生结果，保证界面与库里记录一致。
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        GROUP_RECOMMEND: [],
        GROUP_NEEDS_REVIEW: [],
        GROUP_NOT_RECOMMEND: [],
    }
    for r in sort_results(results):
        g = group_of(r.verdict)
        reason = (reasons or {}).get(r.resume_id) or one_line_reason(r)
        buckets[g].append(
            {
                "resume_id": r.resume_id,
                "file_name": r.file_name,
                "candidate_name": r.candidate_name,
                "candidate_contact": r.candidate_contact,
                "verdict": r.verdict.value,
                "group": g,
                "score": r.score,
                "max_score": r.max_score,
                "reason": reason,
                "hard_filter_blocked": r.hard_filter_blocked,
                "parse_status": r.parse_status.value,
                "notes": list(r.notes),
                "dimensions": [_dimension_view(d) for d in r.dimensions],
            }
        )

    groups = [
        {
            "key": key,
            "label": GROUP_LABELS[key],
            "count": len(buckets[key]),
            "items": buckets[key],
        }
        for key in sorted(buckets, key=lambda k: GROUP_ORDER[k])
    ]
    return {
        "groups": groups,
        "counts": {k: len(v) for k, v in buckets.items()},
        "total": sum(len(v) for v in buckets.values()),
    }


def _dimension_view(d: DimensionResult) -> dict[str, Any]:
    return {
        "id": d.dimension_id,
        "name": d.name,
        "kind": d.kind,
        "judgement": d.judgement.value,
        "score": d.score,
        "reason": d.reason,
        "judged_by": d.judged_by,
        "evidence": [{"quote": e.quote, "source": e.source, "page": e.page} for e in d.evidence],
    }
