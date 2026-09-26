"""排名视图（三分组）与一键复核。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ...core.models import CandidateResult
from ...services import jobs as jobs_service
from ...services.ranking import build_ranking
from ...store import repo
from ...store.db import Database
from ...store.entities import PENDING_STATUSES
from ..deps import get_db
from ..schemas import (
    RankingGroupOut,
    RankingItemOut,
    RankingOut,
    ReviewIn,
    ReviewOut,
)

router = APIRouter(prefix="/api/jobs", tags=["ranking"])


def build_ranking_out(db: Database, job_id: str) -> RankingOut:
    """组装排名响应。分组规则见 services.ranking（三分组，不是一条纯排序）。"""
    records = repo.list_job_resumes(db, job_id)
    pending = sum(1 for r in records if r.status in PENDING_STATUSES)

    info = jobs_service.current_rubric_info(db, job_id)
    if info is None:
        return RankingOut(job_id=job_id, pending=pending)

    rows = repo.list_results(db, job_id, info.rubric_id)
    results: list[CandidateResult] = []
    reasons: dict[str, str] = {}
    decisions: dict[str, str | None] = {}
    for row in rows:
        try:
            results.append(CandidateResult.model_validate_json(row["payload"]))
        except Exception:  # noqa: BLE001 - 单条坏数据不应让整个排名视图打不开
            continue
        reasons[row["resume_id"]] = row["reason_line"]
        decisions[row["resume_id"]] = row["hr_decision"]

    data = build_ranking(results, reasons=reasons)

    groups: list[RankingGroupOut] = []
    for group in data["groups"]:
        items = []
        for item in group["items"]:
            payload = dict(item)
            payload["hr_decision"] = decisions.get(item["resume_id"])
            items.append(RankingItemOut(**payload))
        groups.append(
            RankingGroupOut(
                key=group["key"], label=group["label"], count=group["count"], items=items
            )
        )

    return RankingOut(
        job_id=job_id,
        rubric_id=info.rubric_id,
        rubric_version=info.version,
        groups=groups,
        counts=data["counts"],
        total=data["total"],
        pending=pending,
    )


@router.get("/{job_id}/ranking", response_model=RankingOut)
def get_ranking(job_id: str, db: Database = Depends(get_db)) -> RankingOut:
    jobs_service.require_job(db, job_id)
    return build_ranking_out(db, job_id)


@router.post("/{job_id}/review", response_model=ReviewOut)
def review(job_id: str, payload: ReviewIn, db: Database = Depends(get_db)) -> ReviewOut:
    """一键复核。

    这是"系统以后变准的唯一输入来源"（产品方案 §7）——所以它必须便宜到
    HR 愿意点：一次点击即完成，不需要填表、不需要跳转。
    """
    jobs_service.require_job(db, job_id)
    info = jobs_service.current_rubric_info(db, job_id)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="该岗位尚未设置筛选标准"
        )

    decision = None if payload.decision == "clear" else payload.decision
    ok = repo.set_hr_decision(db, job_id, payload.resume_id, info.rubric_id, decision)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="该简历在当前标准下没有结论"
        )
    return ReviewOut(resume_id=payload.resume_id, hr_decision=decision)
