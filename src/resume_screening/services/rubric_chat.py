"""对话式改标准（技术方案 §7 的三段式）。

```
① propose  自然语言 → 结构化变更清单（调模型）＋ 合规两侧拦截
② impact   变更清单 → 对现有排名的影响（纯计算，不调模型）
③ apply    确认/部分采纳 → 写新版本 → 重排（只重评分，不重新解析）
```

**为什么不让模型直接吐一份新标准**：那样无法向 HR 解释改了什么、无法算影响、
也无法支持"部分采纳"。HR 授权的是"帮我改"，不是"随便改"（产品方案 §6.4）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from ..core.compliance import check_text
from ..core.llm import LLMClient
from ..core.models import CandidateResult
from ..core.prompts import PROPOSE_SYSTEM, PROPOSE_USER_TEMPLATE
from ..core.rubric import Rubric
from ..core.rubric_impact import ImpactEstimate, estimate_impact
from ..core.rubric_patch import (
    Proposal,
    RubricPatchError,
    apply_patches,
    describe_patches,
    parse_proposal,
)
from ..store import repo
from ..store.db import Database
from ..store.entities import ProposalRecord
from . import jobs as jobs_service
from . import rescore as rescore_service


class UtteranceRejected(ValueError):
    """用户这句话本身就不能执行（通常触碰合规红线）。

    单独一个异常类型，是为了让上层给出"拒绝 + 理由"，而不是当成系统故障。
    """


@dataclass
class ProposalOutcome:
    record: ProposalRecord
    proposal: Proposal
    impact: ImpactEstimate
    descriptions: list[str]
    llm_calls: int = 0

    @property
    def refused(self) -> bool:
        return self.proposal.refused


@dataclass
class ApplyOutcome:
    rubric_id: str
    version: str
    applied: int
    rescored: int
    llm_calls: int


# ————————————————— ① 生成提案 —————————————————


def screen_utterance(utterance: str) -> None:
    """**输入侧合规拦截**（技术方案 §7：两侧各拦一次）。

    用户直接说"把 35 岁以上的筛掉"时，必须当场拒绝并说明原因，而不是照做
    （产品方案 §6.5）。挡在调用模型之前，既保证了红线，也省掉一次无意义的调用。

    只在输出侧拦是不够的：模型的"自由发挥"不由用户原话决定，
    而用户的迂回表达也不是输出侧能还原的——所以两侧都要有。
    """
    hits = check_text(utterance, field="HR 的要求")
    if not hits:
        return
    cats = "、".join(sorted({h.category for h in hits}))
    keywords = "、".join(sorted({f"“{h.keyword}”" for h in hits}))
    raise UtteranceRejected(
        f"这个要求不能执行：它涉及受保护属性（{cats}，命中 {keywords}）。"
        "依据《就业促进法》与本项目的合规红线，性别、年龄、婚育、籍贯、民族、健康状况、"
        "政治面貌不得作为筛选条件，也不能通过「排除掉某些人」的方式间接实现。\n"
        "如果你真正想筛的是**能力或经验**，可以直接说那件事本身"
        "（例如「需要独立带过 RAG 项目上线」），我来把它翻译成可判定的口径。"
    )


def _load_results(db: Database, job_id: str, rubric_id: str) -> tuple[list[CandidateResult], int]:
    """取当前标准下已有结论，以及还没有结论的份数。

    没有结论的包括：还在处理中、解析不达标（需人工）、处理失败。
    它们不在排名里，但要在预估里说明"这些人没被算进去"，否则 HR 会以为
    预估已经覆盖了全部简历。
    """
    results: list[CandidateResult] = []
    for row in repo.list_results(db, job_id, rubric_id):
        try:
            results.append(CandidateResult.model_validate_json(row["payload"]))
        except Exception:  # noqa: BLE001 - 单条坏数据不应让整个预估失败
            continue
    not_scored = sum(1 for r in repo.list_job_resumes(db, job_id) if r.status != "done")
    return results, not_scored


async def propose(
    *, db: Database, job_id: str, utterance: str, llm: LLMClient
) -> ProposalOutcome:
    """把一句话变成变更清单 + 影响预估，并存档（不改变当前标准）。"""
    text = (utterance or "").strip()
    if not text:
        raise ValueError("请先说要改什么")

    screen_utterance(text)

    job = jobs_service.require_job(db, job_id)
    rubric, info = jobs_service.require_rubric(db, job_id)

    from ..core.llm import MeteredLLM  # 局部导入：避免把计量细节铺到模块顶部

    metered = MeteredLLM(llm)
    payload = await asyncio.to_thread(
        lambda: metered.complete_json(
            system=PROPOSE_SYSTEM,
            user=PROPOSE_USER_TEMPLATE.format(
                job_title=job.title,
                jd=job.jd_text,
                rubric=json.dumps(rubric.model_dump(mode="json"), ensure_ascii=False, indent=2),
                utterance=text,
            ),
        ).payload
    )

    # 输出侧合规：模型给出的维度名称/口径/正反例/改动理由都过一遍
    proposal = parse_proposal(payload)

    if proposal.refused:
        # 模型判定该要求不能执行（通常是它看出迂回的合规意图）。照样存档，
        # 让 HR 能看到"我提过这个要求、被拒了、理由是什么"。
        impact = ImpactEstimate(notes=[proposal.refused_reason])
    else:
        candidate = apply_patches(rubric, proposal)  # 干跑一次：不合法的清单在这里被拦下
        results, not_scored = _load_results(db, job_id, info.rubric_id)
        impact = estimate_impact(rubric, candidate, results, not_scored=not_scored)

    record = repo.put_proposal(
        db,
        job_id=job_id,
        utterance=text,
        base_rubric_id=info.rubric_id,
        payload=proposal.model_dump(mode="json"),
        impact=impact.to_payload(),
    )
    return ProposalOutcome(
        record=record,
        proposal=proposal,
        impact=impact,
        descriptions=describe_patches(proposal),
        llm_calls=metered.call_count,
    )


# ————————————————— ③ 确认生效 —————————————————


async def apply(
    *,
    db: Database,
    job_id: str,
    proposal_id: str,
    accepted: list[int] | None = None,
    llm: LLMClient,
) -> ApplyOutcome:
    """确认（或部分采纳）后写新版本并重排。

    `accepted=None` 表示全部采纳；给出索引列表表示部分采纳（产品方案 §6.4）。
    重排只重跑评分层，因此"改标准后立即看到新排名"不需要等待重新解析。
    """
    jobs_service.require_job(db, job_id)
    record = repo.get_proposal(db, proposal_id)
    if record is None:
        raise RubricPatchError(f"提案不存在：{proposal_id}")
    if record.job_id != job_id:
        raise RubricPatchError("提案不属于该岗位")

    rubric, info = jobs_service.require_rubric(db, job_id)
    if info.rubric_id != record.base_rubric_id:
        # 基线变了还照旧应用，会把别人后来的改动悄悄覆盖掉
        raise RubricPatchError(
            "当前标准已经变了（不是这份提案的基准版本），为避免覆盖他人的改动，请重新提出一次"
        )

    proposal = Proposal.model_validate(record.payload)
    selected = proposal.select(accepted)
    if selected.refused:
        raise RubricPatchError(f"该要求已被拒绝，无法应用：{selected.refused_reason}")
    if not selected.patches:
        raise RubricPatchError("没有选中任何变更")

    new_rubric = apply_patches(rubric, selected)
    note = f"由对话式改标准生成｜原话：{record.utterance}"
    new_info = jobs_service.set_rubric(db, job_id, new_rubric, note=note)

    stats = await rescore_service.rescore_job(
        db=db, job_id=job_id, rubric=new_rubric, rubric_id=new_info.rubric_id, llm=llm
    )
    return ApplyOutcome(
        rubric_id=new_info.rubric_id,
        version=new_info.version,
        applied=len(selected.patches),
        rescored=stats.rescored,
        llm_calls=stats.llm_calls,
    )


def load_impact(record: ProposalRecord) -> dict[str, Any]:
    """把存档的影响预估取回来（追溯用）。"""
    return record.impact
