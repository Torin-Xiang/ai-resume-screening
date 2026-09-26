"""HTTP 层的请求 / 响应模型。

只描述"接口长什么样"，不含业务规则——业务规则在 `services/`。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ——————————————————————— 岗位 ———————————————————————


class JobCreate(BaseModel):
    title: str = Field(..., min_length=1, description="岗位名称")
    jd_text: str = Field("", description="岗位 JD 原文，由 HR 提供")
    rubric: dict[str, Any] | None = Field(
        None, description="可选的初始筛选标准（不传则该岗位暂无标准，上传后会提示需先设标准）"
    )


class JobUpdate(BaseModel):
    title: str | None = None
    jd_text: str | None = None


class JobOut(BaseModel):
    job_id: str
    title: str
    jd_text: str = ""
    status: str = "open"
    created_at: str = ""
    updated_at: str = ""
    resume_count: int = 0
    pending_count: int = 0
    rubric_version: str | None = None


# ——————————————————— 筛选标准 ———————————————————


class RubricOut(BaseModel):
    rubric_id: str
    job_id: str
    version: str
    payload: dict[str, Any]
    note: str = ""
    created_at: str = ""


class RubricSetIn(BaseModel):
    rubric: dict[str, Any]
    note: str = ""
    rescore: bool = Field(True, description="写入后是否立即用新标准重排（不重新解析）")
    make_current: bool = Field(True, description="是否设为当前标准")


class RubricGenerateIn(BaseModel):
    jd_text: str = Field("", description="留空则用岗位自带的 JD")
    save: bool = Field(False, description="是否生成后直接保存；默认只返回草稿供确认")
    version: str = Field("", description="留空则自动编号")


class RubricGenerateOut(BaseModel):
    rubric: dict[str, Any]
    dropped: list[str] = Field(default_factory=list, description="因合规红线被剔除的维度")
    saved: bool = False
    rubric_id: str | None = None
    version: str | None = None


class RescoreStatsOut(BaseModel):
    total: int = 0
    rescored: int = 0
    skipped_no_doc: int = 0
    skipped_unusable: int = 0
    failed: int = 0
    llm_calls: int = 0
    llm_tokens: int = 0


# ——————————————————— 简历与上传 ———————————————————


class UploadItemOut(BaseModel):
    resume_id: str
    file_name: str
    status: str = "queued"
    is_new: bool = True
    origin: str = "upload"
    error: str = ""


class UploadRejectedOut(BaseModel):
    file_name: str
    reason: str


class UploadResponse(BaseModel):
    accepted: int = 0
    items: list[UploadItemOut] = Field(default_factory=list)
    rejected: list[UploadRejectedOut] = Field(default_factory=list)


class ResumeOut(BaseModel):
    resume_id: str
    file_name: str
    status: str
    error: str = ""
    origin: str = "upload"
    created_at: str = ""
    updated_at: str = ""
    verdict: str | None = None
    group: str | None = None
    reason: str | None = None
    candidate_name: str = ""
    candidate_contact: str = ""


class ResumeListOut(BaseModel):
    items: list[ResumeOut] = Field(default_factory=list)
    pending: int = 0


# ——————————————————— 排名与复核 ———————————————————


class EvidenceOut(BaseModel):
    quote: str
    source: str = ""
    page: int | None = None


class DimensionOut(BaseModel):
    id: str
    name: str
    kind: str
    judgement: str
    score: float = 0
    reason: str = ""
    judged_by: str = "llm"
    evidence: list[EvidenceOut] = Field(default_factory=list)


class RankingItemOut(BaseModel):
    resume_id: str
    file_name: str
    candidate_name: str = ""
    candidate_contact: str = ""
    verdict: str
    group: str
    score: float = 0
    max_score: float = 0
    reason: str = ""
    hard_filter_blocked: bool = False
    parse_status: str = "ok"
    notes: list[str] = Field(default_factory=list)
    dimensions: list[DimensionOut] = Field(default_factory=list)
    hr_decision: str | None = None


class RankingGroupOut(BaseModel):
    key: str
    label: str
    count: int = 0
    items: list[RankingItemOut] = Field(default_factory=list)


class RankingOut(BaseModel):
    job_id: str
    rubric_id: str | None = None
    rubric_version: str | None = None
    groups: list[RankingGroupOut] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    total: int = 0
    pending: int = 0


class ReviewIn(BaseModel):
    resume_id: str
    decision: Literal["agree", "disagree", "clear"] = "agree"
    note: str = ""


class ReviewOut(BaseModel):
    resume_id: str
    hr_decision: str | None = None
