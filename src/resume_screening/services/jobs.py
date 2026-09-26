"""岗位服务：岗位的增删改查与**标准版本管理**（技术方案 §3.1）。

标准版本化的意义（产品方案 §6.4）：每次改动留一版，可回退、可对比。
没有版本号，上周按 A 标准筛、这周按 B 标准筛的结果会混在一起无法复盘。
"""

from __future__ import annotations

from ..core.rubric import Rubric
from ..store import repo
from ..store.db import Database
from ..store.entities import Job, JobStatus, RubricVersionInfo


class JobNotFound(LookupError):
    """岗位不存在。"""


class RubricNotSet(LookupError):
    """岗位尚未设置筛选标准。"""


# ——————————————————————— 岗位 ———————————————————————


def create_job(db: Database, *, title: str, jd_text: str = "") -> Job:
    title = title.strip()
    if not title:
        raise ValueError("岗位名称不能为空")
    return repo.create_job(db, title=title, jd_text=jd_text.strip())


def list_jobs(db: Database, *, include_archived: bool = False) -> list[Job]:
    return repo.list_jobs(db, include_archived=include_archived)


def require_job(db: Database, job_id: str) -> Job:
    job = repo.get_job(db, job_id)
    if job is None:
        raise JobNotFound(job_id)
    return job


def update_job(
    db: Database, job_id: str, *, title: str | None = None, jd_text: str | None = None
) -> Job:
    require_job(db, job_id)
    repo.update_job(db, job_id, title=title, jd_text=jd_text)
    return require_job(db, job_id)


def set_job_status(db: Database, job_id: str, status: JobStatus | str) -> Job:
    require_job(db, job_id)
    value = status.value if isinstance(status, JobStatus) else status
    repo.update_job(db, job_id, status=value)
    return require_job(db, job_id)


def delete_job(db: Database, job_id: str) -> None:
    require_job(db, job_id)
    repo.delete_job(db, job_id)


# ——————————————————— 标准版本管理 ———————————————————


def list_rubric_versions(db: Database, job_id: str) -> list[RubricVersionInfo]:
    return repo.list_rubric_versions(db, job_id)


def current_rubric_info(db: Database, job_id: str) -> RubricVersionInfo | None:
    return repo.get_current_rubric(db, job_id)


def current_rubric(db: Database, job_id: str) -> Rubric | None:
    info = current_rubric_info(db, job_id)
    return Rubric.model_validate(info.payload) if info else None


def require_rubric(db: Database, job_id: str) -> tuple[Rubric, RubricVersionInfo]:
    info = current_rubric_info(db, job_id)
    if info is None:
        raise RubricNotSet(f"岗位 {job_id} 尚未设置筛选标准")
    return Rubric.model_validate(info.payload), info


def _auto_version(existing: list[str]) -> str:
    i = 1
    while f"v{i}" in existing:
        i += 1
    return f"v{i}"


def _dedupe_version(existing: list[str], proposed: str) -> str:
    if proposed not in existing:
        return proposed
    i = 2
    while f"{proposed}-{i}" in existing:
        i += 1
    return f"{proposed}-{i}"


def set_rubric(db: Database, job_id: str, rubric: Rubric, *, note: str = "") -> RubricVersionInfo:
    """写入一份新标准，成为该岗位的当前标准。

    - 标准版本号冲突时自动追加序号，**不覆盖历史版本**
    - 调用方需自行决定是否重排（`services.rescore`）
    """
    require_job(db, job_id)
    existing = [v.version for v in repo.list_rubric_versions(db, job_id)]
    proposed = (rubric.version or "").strip()
    if not proposed:
        version = _auto_version(existing)
    else:
        version = _dedupe_version(existing, proposed)

    rubric.version = version
    return repo.add_rubric_version(
        db,
        job_id=job_id,
        rubric_payload=rubric.model_dump(mode="json"),
        version=version,
        note=note,
    )
