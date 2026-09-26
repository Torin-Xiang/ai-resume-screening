"""存储层实体与状态枚举（技术方案 §4）。

这些是**持久化实体**，不是判断内核的数据结构（后者在 `core/models.py`）。
刻意分开：`core` 不认识数据库，`store` 不认识评分逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ResumeStatus(str, Enum):
    """简历处理状态机（技术方案 §4.2）。

    queued → parsing → extracting → scoring → done
                  ↓         ↓           ↓
            needs_manual（无文本层，不送模型）
            failed（解析或调用异常）

    前端只渲染 `done` 的记录，因此"处理中不显示分数"（产品方案 §6.1）
    在数据层就被保证，不需要界面额外判断。
    """

    QUEUED = "queued"
    PARSING = "parsing"
    EXTRACTING = "extracting"
    SCORING = "scoring"
    DONE = "done"
    NEEDS_MANUAL = "needs_manual"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (ResumeStatus.DONE, ResumeStatus.NEEDS_MANUAL, ResumeStatus.FAILED)

    @property
    def is_pending(self) -> bool:
        return not self.is_terminal


PENDING_STATUSES = tuple(s.value for s in ResumeStatus if s.is_pending)


class JobStatus(str, Enum):
    OPEN = "open"
    ARCHIVED = "archived"


@dataclass
class Job:
    job_id: str
    title: str
    jd_text: str = ""
    status: str = JobStatus.OPEN.value
    created_at: str = ""
    updated_at: str = ""
    # 派生统计（列表页展示用，不落库）
    resume_count: int = 0
    pending_count: int = 0

    @property
    def is_archived(self) -> bool:
        return self.status == JobStatus.ARCHIVED.value


@dataclass
class RubricVersionInfo:
    """标准的一个版本。`payload` 为完整 Rubric JSON。"""

    rubric_id: str
    job_id: str
    version: str
    payload: dict[str, Any]
    note: str = ""
    created_at: str = ""


@dataclass
class JobResume:
    job_id: str
    resume_id: str
    file_name: str
    stored_path: str
    origin: str = "upload"
    status: str = ResumeStatus.QUEUED.value
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    # 派生：当前标准下的结论（可能还没算出来）
    result: dict[str, Any] | None = field(default=None, repr=False)


@dataclass
class ResumeDocRecord:
    resume_id: str
    doc_payload: dict[str, Any]
    parse_status: str
    created_at: str = ""


@dataclass
class ProposalRecord:
    """一次"用自然语言改标准"的完整记录（技术方案 §7）。

    `impact` 与 `payload` 一起存，是为了在 HR 回过头来看时能回答
    "当时告诉我会影响 3 个人，后来到底是哪 3 个"——只存结论不存依据，
    追溯就变成一句空话。
    """

    proposal_id: str
    job_id: str
    utterance: str
    base_rubric_id: str
    payload: dict[str, Any]
    impact: dict[str, Any]
    created_at: str = ""
