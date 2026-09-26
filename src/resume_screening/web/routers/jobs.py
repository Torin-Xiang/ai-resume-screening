"""岗位与筛选标准的接口。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, status

from ...core.rubric import Rubric
from ...core.rubric_gen import generate_rubric
from ...core.rubric_patch import RubricPatchError
from ...services import jobs as jobs_service
from ...services import rescore as rescore_service
from ...services import rubric_chat
from ...store import repo
from ...store.db import Database
from ...store.entities import Job
from ..deps import get_db, get_llm
from ..schemas import (
    ImpactOut,
    JobCreate,
    JobOut,
    JobUpdate,
    PatchOut,
    RescoreStatsOut,
    RubricApplyIn,
    RubricApplyOut,
    RubricGenerateIn,
    RubricGenerateOut,
    RubricOut,
    RubricProposeIn,
    RubricProposeOut,
    RubricRollbackIn,
    RubricSetIn,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _parse_rubric(payload: dict) -> Rubric:
    """把外部传入的 JSON 变成 Rubric，格式或合规不通过时返回 422 而不是 500。"""
    try:
        return Rubric.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - pydantic 校验失败与合规拦截都归为"入参不合法"
        # 直接用数字状态码：Starlette 对 HTTP_422_UNPROCESSABLE_ENTITY 常量已弃用，
        # 而不同版本的新常量名不一致，写数字最稳妥。
        raise HTTPException(status_code=422, detail=f"筛选标准不合法：{exc}") from exc


def _to_job_out(db: Database, job: Job) -> JobOut:
    info = jobs_service.current_rubric_info(db, job.job_id)
    return JobOut(
        job_id=job.job_id,
        title=job.title,
        jd_text=job.jd_text,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        resume_count=job.resume_count,
        pending_count=job.pending_count,
        rubric_version=info.version if info else None,
    )


def _to_rubric_out(info) -> RubricOut:
    return RubricOut(
        rubric_id=info.rubric_id,
        job_id=info.job_id,
        version=info.version,
        payload=info.payload,
        note=info.note,
        created_at=info.created_at,
    )


@router.get("", response_model=list[JobOut])
def list_jobs(include_archived: bool = False, db: Database = Depends(get_db)) -> list[JobOut]:
    return [_to_job_out(db, j) for j in jobs_service.list_jobs(db, include_archived=include_archived)]


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, db: Database = Depends(get_db)) -> JobOut:
    try:
        job = jobs_service.create_job(db, title=payload.title, jd_text=payload.jd_text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.rubric:
        jobs_service.set_rubric(db, job.job_id, _parse_rubric(payload.rubric), note="建岗时提供")
    return _to_job_out(db, jobs_service.require_job(db, job.job_id))


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Database = Depends(get_db)) -> JobOut:
    return _to_job_out(db, jobs_service.require_job(db, job_id))


@router.patch("/{job_id}", response_model=JobOut)
def update_job(job_id: str, payload: JobUpdate, db: Database = Depends(get_db)) -> JobOut:
    job = jobs_service.update_job(db, job_id, title=payload.title, jd_text=payload.jd_text)
    return _to_job_out(db, job)


@router.post("/{job_id}/archive", response_model=JobOut)
def archive_job(job_id: str, db: Database = Depends(get_db)) -> JobOut:
    job = jobs_service.set_job_status(db, job_id, "archived")
    return _to_job_out(db, job)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: str, db: Database = Depends(get_db)) -> None:
    jobs_service.delete_job(db, job_id)


# ——————————————————— 筛选标准 ———————————————————


@router.get("/{job_id}/rubric", response_model=RubricOut)
def get_rubric(job_id: str, db: Database = Depends(get_db)) -> RubricOut:
    jobs_service.require_job(db, job_id)
    info = jobs_service.current_rubric_info(db, job_id)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="该岗位尚未设置筛选标准"
        )
    return _to_rubric_out(info)


@router.put("/{job_id}/rubric", response_model=RubricOut)
async def set_rubric(
    job_id: str,
    payload: RubricSetIn,
    db: Database = Depends(get_db),
    llm=Depends(get_llm),
) -> RubricOut:
    """写入新标准。默认立即重排——**只重跑评分层，不重新解析**（技术方案 §7）。"""
    jobs_service.require_job(db, job_id)
    rubric = _parse_rubric(payload.rubric)
    info = jobs_service.set_rubric(db, job_id, rubric, note=payload.note)
    if payload.rescore:
        await rescore_service.rescore_job(
            db=db, job_id=job_id, rubric=rubric, rubric_id=info.rubric_id, llm=llm
        )
    return _to_rubric_out(info)


@router.get("/{job_id}/rubric/versions", response_model=list[RubricOut])
def list_rubric_versions(job_id: str, db: Database = Depends(get_db)) -> list[RubricOut]:
    jobs_service.require_job(db, job_id)
    return [_to_rubric_out(v) for v in jobs_service.list_rubric_versions(db, job_id)]


@router.post("/{job_id}/rubric/generate", response_model=RubricGenerateOut)
async def generate_rubric_draft(
    job_id: str,
    payload: RubricGenerateIn,
    db: Database = Depends(get_db),
    llm=Depends(get_llm),
) -> RubricGenerateOut:
    """从 JD 生成一份标准**草稿**。

    默认只返回草稿不落库——生成出来的判定口径（`definition` 与正反例）必须经
    HR 确认才生效，这是"改标准必须确认后生效"（产品方案 §6.4）在建岗这一步的延伸。
    """
    job = jobs_service.require_job(db, job_id)
    jd = (payload.jd_text or job.jd_text).strip()
    if not jd:
        raise HTTPException(status_code=422, detail="JD 为空，无法生成筛选标准")

    rubric, dropped = await asyncio.to_thread(
        generate_rubric, jd, llm, version=payload.version or None, job_title=job.title
    )

    if not payload.save:
        return RubricGenerateOut(
            rubric=rubric.model_dump(mode="json"), dropped=dropped, saved=False
        )

    info = jobs_service.set_rubric(db, job_id, rubric, note="由 JD 自动生成")
    return RubricGenerateOut(
        rubric=info.payload, dropped=dropped, saved=True, rubric_id=info.rubric_id, version=info.version
    )


# ————————————— 对话式改标准（技术方案 §7）—————————————


@router.post("/{job_id}/rubric/propose", response_model=RubricProposeOut)
async def propose_rubric_change(
    job_id: str,
    payload: RubricProposeIn,
    db: Database = Depends(get_db),
    llm=Depends(get_llm),
) -> RubricProposeOut:
    """用一句话改标准 → 变更清单 + 对现有排名的影响。**这一步不改变当前标准。**

    三道闸的第一、二道（产品方案 §6.4）：先说清改了什么、会影响到谁，
    再由 HR 决定要不要生效。

    触碰合规红线时**返回 200 + refused=true**，而不是 4xx：那是产品对
    "这个要求不能做"的正常回答，界面要把它显示在输入框旁边给出路，
    而不是当成一次请求失败丢到页面顶部（产品方案 §6.5）。
    422 只留给真正的问题：原话为空、清单与当前标准对不上。
    """
    jobs_service.require_job(db, job_id)
    try:
        outcome = await rubric_chat.propose(
            db=db, job_id=job_id, utterance=payload.utterance, llm=llm
        )
    except RubricPatchError as exc:
        raise HTTPException(status_code=422, detail=f"这次改动无法执行：{exc}") from exc

    descriptions = outcome.descriptions
    patches = [
        PatchOut(
            index=i,
            op=p.op,
            kind=p.kind,
            target_id=p.target_id,
            before=p.before,
            after=p.after,
            reason=p.reason,
            description=descriptions[i] if i < len(descriptions) else "",
        )
        for i, p in enumerate(outcome.proposal.patches)
    ]
    return RubricProposeOut(
        proposal_id=outcome.record.proposal_id,
        utterance=outcome.record.utterance,
        summary=outcome.proposal.summary,
        refused=outcome.refused,
        refused_reason=outcome.proposal.refused_reason,
        patches=patches,
        impact=ImpactOut(**outcome.impact.to_payload()),
    )


@router.post("/{job_id}/rubric/apply", response_model=RubricApplyOut)
async def apply_rubric_change(
    job_id: str,
    payload: RubricApplyIn,
    db: Database = Depends(get_db),
    llm=Depends(get_llm),
) -> RubricApplyOut:
    """第三道闸：确认（或部分采纳）后写入新版本并重排。

    重排只重跑评分层，不重新解析——所以"改完立刻看到新排名"不需要等。
    """
    try:
        outcome = await rubric_chat.apply(
            db=db,
            job_id=job_id,
            proposal_id=payload.proposal_id,
            accepted=payload.accepted,
            llm=llm,
        )
    except RubricPatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return RubricApplyOut(
        rubric_id=outcome.rubric_id,
        version=outcome.version,
        applied=outcome.applied,
        rescored=outcome.rescored,
        llm_calls=outcome.llm_calls,
    )


@router.post("/{job_id}/rubric/rollback", response_model=RubricOut)
async def rollback_rubric(
    job_id: str,
    payload: RubricRollbackIn,
    db: Database = Depends(get_db),
    llm=Depends(get_llm),
) -> RubricOut:
    """回滚到历史版本（产品方案 §6.4：标准每次变更留版本，可回退）。

    回滚**不删历史**：它是"把旧版本重新设为当前版本"，而不是把时间倒回去。
    这样"改了又改回来"这件事本身也留在版本记录里，复盘时看得到。
    """
    jobs_service.require_job(db, job_id)
    target = repo.get_rubric_by_id(db, payload.rubric_id)
    if target is None or target.job_id != job_id:
        raise HTTPException(status_code=404, detail="该版本不存在或不属于此岗位")

    rubric = _parse_rubric(target.payload)
    info = jobs_service.set_rubric(db, job_id, rubric, note=f"回滚到 {target.version}")
    if payload.rescore:
        await rescore_service.rescore_job(
            db=db, job_id=job_id, rubric=rubric, rubric_id=info.rubric_id, llm=llm
        )
    return _to_rubric_out(info)


@router.post("/{job_id}/ranking/rebuild", response_model=RescoreStatsOut)
async def rebuild_ranking(
    job_id: str, db: Database = Depends(get_db), llm=Depends(get_llm)
) -> RescoreStatsOut:
    """按**当前标准**重排。不重新解析、不重新提取。"""
    rubric, info = jobs_service.require_rubric(db, job_id)
    stats = await rescore_service.rescore_job(
        db=db, job_id=job_id, rubric=rubric, rubric_id=info.rubric_id, llm=llm
    )
    return RescoreStatsOut(**stats.__dict__)
