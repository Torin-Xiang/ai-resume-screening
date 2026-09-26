"""简历上传、列表与移除。

上传接口立刻返回 202，**不在请求内做解析**（技术方案 §6）：
一份简历十几秒、十份就超时，用户会看到转圈然后失败——那正是要避免的体验。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from ...services import ingest as ingest_service
from ...services import jobs as jobs_service
from ...services.ranking import group_of
from ...store import repo
from ...store.db import Database
from ...store.entities import PENDING_STATUSES
from ..deps import get_db, get_worker
from ..schemas import (
    PasteTextIn,
    ResumeListOut,
    ResumeOut,
    UploadItemOut,
    UploadRejectedOut,
    UploadResponse,
)

router = APIRouter(prefix="/api/jobs/{job_id}/resumes", tags=["resumes"])


@router.post("", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_resumes(
    job_id: str,
    files: list[UploadFile] = File(..., description="可一次传多份，支持混合"),
    db: Database = Depends(get_db),
    worker=Depends(get_worker),
) -> UploadResponse:
    """接收简历：落盘 → 建关联 → 入队，然后立即返回。

    返回体里每份都是 `queued`，真实处理进度通过 SSE 推送。
    格式不支持的文件**不静默丢弃**，单独列在 `rejected` 里告诉用户原因。
    """
    jobs_service.require_job(db, job_id)

    items: list[UploadItemOut] = []
    rejected: list[UploadRejectedOut] = []

    for upload in files:
        file_name = upload.filename or "unnamed.pdf"
        content = await upload.read()
        if not content:
            rejected.append(UploadRejectedOut(file_name=file_name, reason="文件为空"))
            continue
        try:
            result = ingest_service.ingest_upload(
                db, job_id=job_id, file_name=file_name, content=content
            )
        except ingest_service.UnsupportedFormat as exc:
            rejected.append(UploadRejectedOut(file_name=file_name, reason=str(exc)))
            continue

        items.append(
            UploadItemOut(
                resume_id=result.resume_id,
                file_name=result.file_name,
                status="queued",
                is_new=result.is_new,
                origin=result.origin,
            )
        )
        worker.enqueue(job_id, result.resume_id)

    return UploadResponse(accepted=len(items), items=items, rejected=rejected)


@router.post("/text", response_model=UploadItemOut, status_code=status.HTTP_202_ACCEPTED)
async def paste_resume_text(
    job_id: str,
    payload: PasteTextIn,
    db: Database = Depends(get_db),
    worker=Depends(get_worker),
) -> UploadItemOut:
    """粘贴文本兜底：扫描件没有文本层时给 HR 的出路（产品方案 §6.1、P0#9）。

    这是"绝不允许静默跳过"的落地方式——解析失败的人必须有一条能走到排名里的路，
    否则 HR 会以为系统看过并放过了他。粘完立即入队，与上传同样的"上传即处理"。
    """
    jobs_service.require_job(db, job_id)
    try:
        result = ingest_service.ingest_text(
            db, job_id=job_id, text=payload.text, file_name=payload.file_name
        )
    except ingest_service.UnsupportedFormat as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    worker.enqueue(job_id, result.resume_id)
    return UploadItemOut(
        resume_id=result.resume_id,
        file_name=result.file_name,
        status="queued",
        is_new=result.is_new,
        origin=result.origin,
    )


@router.get("", response_model=ResumeListOut)
def list_resumes(job_id: str, db: Database = Depends(get_db)) -> ResumeListOut:
    """简历列表与处理状态。含结论的只有 `done` 的那些（处理中不给分数）。"""
    jobs_service.require_job(db, job_id)
    records = repo.list_job_resumes(db, job_id)

    info = jobs_service.current_rubric_info(db, job_id)
    verdicts: dict[str, tuple[str, str]] = {}
    if info is not None:
        for row in repo.list_results(db, job_id, info.rubric_id):
            verdicts[row["resume_id"]] = (row["verdict"], row["reason_line"])

    items: list[ResumeOut] = []
    for record in records:
        hit = verdicts.get(record.resume_id)
        items.append(
            ResumeOut(
                resume_id=record.resume_id,
                file_name=record.file_name,
                status=record.status,
                error=record.error,
                origin=record.origin,
                created_at=record.created_at,
                updated_at=record.updated_at,
                verdict=hit[0] if hit else None,
                group=group_of(hit[0]) if hit else None,
                reason=hit[1] if hit else None,
            )
        )

    pending = sum(1 for r in records if r.status in PENDING_STATUSES)
    return ResumeListOut(items=items, pending=pending)


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_resume(job_id: str, resume_id: str, db: Database = Depends(get_db)) -> None:
    if not repo.remove_job_resume(db, job_id, resume_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="该简历不在此岗位下")
