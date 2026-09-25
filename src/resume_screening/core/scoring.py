"""双层评估：规则判硬门槛 + LLM 只做软性判断（方案 §2.1 ④、§4.2、§4.3）。

三条硬性原则：
1. **硬门槛优先走规则**；只有规则判不了的软性维度才交给 LLM。
2. **区分「不满足」与「未提及」**：未提及/无法判定 → 标"待确认"，**绝不淘汰**。
3. **LLM 的结论必须带原文引用**：证据不命中原文 → 判定作废（→ 待确认）；
   理由触碰合规红线 → 判定作废。宁可判不出来，也不让不可追溯的结论进入结果。
"""

from __future__ import annotations

from typing import Any

from .compliance import scan_text
from .evidence import quote_in_text
from .llm import LLMClient, MeteredLLM
from .models import (
    CandidateResult,
    DimensionResult,
    Evidence,
    Judgement,
    ResumeDocument,
    RunRecord,
    ScreeningVerdict,
)
from .prompts import SCORE_SYSTEM, SCORE_USER_TEMPLATE
from .rubric import Bonus, HardFilter, RedFlag, Rubric
from .rules import run_rule

_JUDGEMENT_MAP = {
    "satisfied": Judgement.SATISFIED,
    "not_satisfied": Judgement.NOT_SATISFIED,
    "not_mentioned": Judgement.NOT_MENTIONED,
    "unknown": Judgement.UNKNOWN,
}


def _render_dimensions(dims: list[Any]) -> str:
    lines: list[str] = []
    for d in dims:
        lines.append(f"- id={d.id}｜{d.name}｜类型={d.kind}")
        lines.append(f"  口径 definition：{d.definition}")
        if d.positive_examples:
            lines.append(f"  正例：{'；'.join(d.positive_examples)}")
        if d.negative_examples:
            lines.append(f"  反例：{'；'.join(d.negative_examples)}")
    return "\n".join(lines)


def _llm_judge(doc: ResumeDocument, dims: list[Any], llm: LLMClient) -> dict[str, dict[str, Any]]:
    """一次性让模型对所有软性维度给出判定（省调用次数，也保证口径一致）。"""
    if not dims:
        return {}
    resp = llm.complete_json(
        system=SCORE_SYSTEM,
        user=SCORE_USER_TEMPLATE.format(
            job_title=doc.extras.get("job_title", ""),
            dimensions=_render_dimensions(dims),
            text=doc.raw_text,
        ),
    )
    out: dict[str, dict[str, Any]] = {}
    for item in (resp.payload or {}).get("results", []) or []:
        if isinstance(item, dict) and item.get("id"):
            out[str(item["id"])] = item
    return out


def _evidence_from_llm(item: dict[str, Any], raw_text: str) -> tuple[list[Evidence], bool]:
    """解析并校验证据；返回 (有效证据列表, 是否全部无效)。"""
    evidences: list[Evidence] = []
    invalid = False
    raw_list = item.get("evidence") or []
    if isinstance(raw_list, dict):
        raw_list = [raw_list]
    for ev in raw_list:
        quote = (ev or {}).get("quote", "") if isinstance(ev, dict) else ""
        quote = str(quote).strip()
        if not quote:
            continue
        if quote_in_text(quote, raw_text):
            evidences.append(Evidence(quote=quote, page=(ev or {}).get("page")))
        else:
            invalid = True
    return evidences, invalid


def _hit_level(item: dict[str, Any], judgement: Judgement) -> float:
    try:
        lv = float(item.get("hit_level", 1))
    except (TypeError, ValueError):
        lv = 1.0
    lv = min(max(lv, 0.0), 1.0)
    if judgement is Judgement.NOT_SATISFIED:
        return 0.0
    if judgement is not Judgement.SATISFIED:
        return 0.0
    return lv


def evaluate_candidate(
    doc: ResumeDocument,
    rubric: Rubric,
    llm: LLMClient,
    *,
    job_title: str = "",
) -> CandidateResult:
    """评估单份简历。"""
    doc.extras.setdefault("job_title", job_title or rubric.job_title)
    result = CandidateResult(
        resume_id=doc.resume_id,
        file_name=doc.file_name,
        source_path=doc.source_path,
        verdict=ScreeningVerdict.NEEDS_REVIEW,
        max_score=rubric.max_possible_score(),
        parse_status=doc.parse_quality.status,
    )

    # 解析质量不达标 → 不评估，直接待人工（方案 §9）
    if not doc.parse_quality.usable:
        result.notes.append(
            "解析质量不达标（扫描件/无文本层）：未评估，需人工处理——" + "；".join(doc.parse_quality.reasons)
        )
        return result

    # ① 硬门槛：能规则化的走规则
    soft_dims: list[Any] = []
    for hf in rubric.hard_filters:
        if hf.rule:
            judgement, reason, evidence = run_rule(hf.rule, doc, hf.params)
            result.dimensions.append(
                DimensionResult(
                    dimension_id=hf.id,
                    name=hf.name,
                    kind="hard_filter",
                    judgement=judgement,
                    reason=reason,
                    evidence=[evidence] if evidence else [],
                    judged_by="rule",
                )
            )
        else:
            soft_dims.append(hf)

    # ② 软性维度（含无规则的硬门槛、加分项、减分项）交给 LLM
    soft_dims = soft_dims + list(rubric.bonus) + list(rubric.red_flags)
    llm_results = _llm_judge(doc, soft_dims, llm)

    score = 0.0
    for dim in soft_dims:
        item = llm_results.get(dim.id)
        if item is None:
            result.dimensions.append(
                DimensionResult(
                    dimension_id=dim.id,
                    name=dim.name,
                    kind=dim.kind,
                    judgement=Judgement.UNKNOWN,
                    reason="模型未返回该维度的判定（待确认）",
                    judged_by="llm",
                )
            )
            continue

        judgement = _JUDGEMENT_MAP.get(str(item.get("judgement", "")).lower(), Judgement.UNKNOWN)
        reason = str(item.get("reason") or "")
        evidences, has_invalid_quote = _evidence_from_llm(item, doc.raw_text)

        # 合规拦截：模型理由里出现受保护属性 → 判定作废
        if scan_text(reason):
            result.dimensions.append(
                DimensionResult(
                    dimension_id=dim.id,
                    name=dim.name,
                    kind=dim.kind,
                    judgement=Judgement.UNKNOWN,
                    reason="判定理由触碰合规红线（受保护属性），已作废",
                    judged_by="llm",
                )
            )
            result.notes.append(f"[{dim.id}] 判定理由触碰合规红线，已作废")
            continue

        # 证据校验：引用必须命中原文，否则判定作废
        if judgement in (Judgement.SATISFIED, Judgement.NOT_SATISFIED):
            if not evidences:
                judgement = Judgement.UNKNOWN
                reason = f"{reason}（无有效原文引用，判定作废→待确认）"
                result.notes.append(f"[{dim.id}] 证据未命中原文，判定作废")
            elif has_invalid_quote:
                result.notes.append(f"[{dim.id}] 部分证据未命中原文，已剔除")

        dim_score = 0.0
        if isinstance(dim, Bonus):
            dim_score = dim.weight * _hit_level(item, judgement)
            score += dim_score
        elif isinstance(dim, RedFlag):
            if judgement is Judgement.SATISFIED:
                dim_score = -dim.penalty
                score -= dim.penalty

        result.dimensions.append(
            DimensionResult(
                dimension_id=dim.id,
                name=dim.name,
                kind=dim.kind,
                judgement=judgement,
                score=round(dim_score, 2),
                reason=reason,
                evidence=evidences,
                judged_by="llm",
            )
        )

    # ③ 汇总结论：只有明确"不满足"的硬门槛才能淘汰
    blocking = [d for d in result.dimensions if d.kind == "hard_filter" and d.judgement is Judgement.NOT_SATISFIED]
    unclear = [
        d
        for d in result.dimensions
        if d.kind == "hard_filter" and d.judgement in (Judgement.NOT_MENTIONED, Judgement.UNKNOWN)
    ]

    if blocking:
        result.verdict = ScreeningVerdict.REJECT
        result.hard_filter_blocked = True
        result.notes.insert(0, "硬门槛未满足：" + "、".join(d.name for d in blocking))
    elif unclear:
        result.verdict = ScreeningVerdict.NEEDS_REVIEW
        result.notes.insert(0, "存在无法判定的硬门槛，需人工确认：" + "、".join(d.name for d in unclear))
    else:
        result.verdict = ScreeningVerdict.PASS

    result.score = round(max(0.0, min(score, rubric.max_possible_score())), 2)
    return result


def _sort_key(r: CandidateResult) -> tuple[int, float]:
    order = {
        ScreeningVerdict.PASS: 0,
        ScreeningVerdict.NEEDS_REVIEW: 1,
        ScreeningVerdict.REJECT: 2,
    }
    return (order[r.verdict], -r.score)


def screen(
    documents: list[ResumeDocument],
    rubric: Rubric,
    llm: LLMClient,
    *,
    run_id: str,
    rubric_path: str = "",
    input_dir: str = "",
) -> tuple[RunRecord, dict[str, int]]:
    """批量筛选：返回 (RunRecord, LLM 计量快照)。"""
    from .ids import new_run_id

    metered = MeteredLLM(llm)
    results = [evaluate_candidate(doc, rubric, metered) for doc in documents]
    results.sort(key=_sort_key)
    run = RunRecord(
        run_id=run_id or new_run_id(),
        rubric_version=rubric.version,
        rubric_path=str(rubric_path),
        job_title=rubric.job_title,
        input_dir=input_dir,
        results=results,
        llm_call_count=metered.call_count,
        llm_cost_tokens=metered.total_tokens,
    )
    return run, metered.snapshot()
