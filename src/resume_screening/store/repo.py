"""仓储：岗位 / 标准版本 / 解析缓存 / 简历关联 / 结论。

层次约定：本模块只做数据存取与最小一致性保证，**不含业务判断**
（比如"改标准后要不要重排"属于 services 层的决策）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Sequence

from ..core.ids import new_id
from ..core.models import CandidateResult, ResumeDocument
from .db import Database
from .entities import (
    PENDING_STATUSES,
    Job,
    JobResume,
    JobStatus,
    ProposalRecord,
    ResumeStatus,
    RubricVersionInfo,
    now_iso,
)


def _pending_placeholders() -> str:
    return ",".join("?" * len(PENDING_STATUSES))


# ——————————————————————— 岗位 ———————————————————————


def create_job(db: Database, *, title: str, jd_text: str = "") -> Job:
    job_id = new_id("job_")
    ts = now_iso()
    db.execute(
        "INSERT INTO jobs (job_id, title, jd_text, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, title, jd_text, JobStatus.OPEN.value, ts, ts),
    )
    return Job(job_id=job_id, title=title, jd_text=jd_text, created_at=ts, updated_at=ts)


def _row_to_job(row: sqlite3.Row) -> Job:
    keys = row.keys()
    return Job(
        job_id=row["job_id"],
        title=row["title"],
        jd_text=row["jd_text"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        resume_count=row["resume_count"] if "resume_count" in keys else 0,
        pending_count=row["pending_count"] if "pending_count" in keys else 0,
    )


_JOB_SELECT = f"""
SELECT j.*,
  (SELECT COUNT(*) FROM job_resumes r WHERE r.job_id = j.job_id) AS resume_count,
  (SELECT COUNT(*) FROM job_resumes r WHERE r.job_id = j.job_id
     AND r.status IN ({_pending_placeholders()})) AS pending_count
FROM jobs j
"""


def list_jobs(db: Database, *, include_archived: bool = False) -> list[Job]:
    sql = _JOB_SELECT
    params: list[Any] = list(PENDING_STATUSES)
    if not include_archived:
        sql += " WHERE j.status = ?"
        params.append(JobStatus.OPEN.value)
    sql += " ORDER BY j.created_at DESC, j.rowid DESC"
    return [_row_to_job(r) for r in db.query(sql, params)]


def get_job(db: Database, job_id: str) -> Job | None:
    sql = _JOB_SELECT + " WHERE j.job_id = ?"
    params: list[Any] = [*PENDING_STATUSES, job_id]
    row = db.query_one(sql, params)
    return _row_to_job(row) if row else None


def update_job(
    db: Database,
    job_id: str,
    *,
    title: str | None = None,
    jd_text: str | None = None,
    status: str | None = None,
) -> bool:
    sets: list[str] = []
    params: list[Any] = []
    for col, val in (("title", title), ("jd_text", jd_text), ("status", status)):
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    if not sets:
        return False
    sets.append("updated_at = ?")
    params.extend([now_iso(), job_id])
    with db.tx() as conn:
        cur = conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?", params)
    return cur.rowcount > 0


def delete_job(db: Database, job_id: str) -> bool:
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    return cur.rowcount > 0


# ——————————————————— 标准版本 ———————————————————


def add_rubric_version(
    db: Database, *, job_id: str, rubric_payload: dict[str, Any], version: str, note: str = ""
) -> RubricVersionInfo:
    rubric_id = new_id("rub_")
    ts = now_iso()
    db.execute(
        "INSERT INTO rubric_versions (rubric_id, job_id, version, payload, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (rubric_id, job_id, version, json.dumps(rubric_payload, ensure_ascii=False), note, ts),
    )
    return RubricVersionInfo(
        rubric_id=rubric_id, job_id=job_id, version=version, payload=rubric_payload, note=note, created_at=ts
    )


def _row_to_rubric(row: sqlite3.Row) -> RubricVersionInfo:
    return RubricVersionInfo(
        rubric_id=row["rubric_id"],
        job_id=row["job_id"],
        version=row["version"],
        payload=json.loads(row["payload"]),
        note=row["note"],
        created_at=row["created_at"],
    )


def get_current_rubric(db: Database, job_id: str) -> RubricVersionInfo | None:
    row = db.query_one(
        "SELECT * FROM rubric_versions WHERE job_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (job_id,),
    )
    return _row_to_rubric(row) if row else None


def get_rubric_by_id(db: Database, rubric_id: str) -> RubricVersionInfo | None:
    row = db.query_one("SELECT * FROM rubric_versions WHERE rubric_id = ?", (rubric_id,))
    return _row_to_rubric(row) if row else None


def list_rubric_versions(db: Database, job_id: str) -> list[RubricVersionInfo]:
    rows = db.query(
        "SELECT * FROM rubric_versions WHERE job_id = ? ORDER BY created_at DESC, rowid DESC",
        (job_id,),
    )
    return [_row_to_rubric(r) for r in rows]


# ——————————————————— 解析缓存 ———————————————————


def get_resume_doc(db: Database, resume_id: str) -> ResumeDocument | None:
    """读取解析+提取结果。与岗位无关，跨岗位共享（技术方案 §4.1）。"""
    row = db.query_one("SELECT doc_payload FROM resume_docs WHERE resume_id = ?", (resume_id,))
    if not row:
        return None
    try:
        return ResumeDocument.model_validate_json(row["doc_payload"])
    except Exception:
        # 缓存损坏或结构不兼容时不阻断链路，按未命中处理，后续重新解析
        return None


def put_resume_doc(db: Database, doc: ResumeDocument) -> None:
    db.execute(
        "INSERT OR REPLACE INTO resume_docs (resume_id, doc_payload, parse_status, created_at)"
        " VALUES (?, ?, ?, ?)",
        (doc.resume_id, doc.model_dump_json(), doc.parse_quality.status.value, now_iso()),
    )


# ——————————————————— 简历关联 ———————————————————


def _row_to_job_resume(row: sqlite3.Row) -> JobResume:
    return JobResume(
        job_id=row["job_id"],
        resume_id=row["resume_id"],
        file_name=row["file_name"],
        stored_path=row["stored_path"],
        origin=row["origin"],
        status=row["status"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def upsert_job_resume(
    db: Database,
    *,
    job_id: str,
    resume_id: str,
    file_name: str,
    stored_path: str,
    origin: str = "upload",
    status: str = ResumeStatus.QUEUED.value,
) -> bool:
    """关联简历到岗位。返回 True 表示是新增（False 表示已存在、仅重置状态）。

    重复上传同一份文件（内容 hash 相同）不应产生两条记录，也不应重复计费。
    """
    ts = now_iso()
    with db.tx() as conn:
        existed = conn.execute(
            "SELECT 1 FROM job_resumes WHERE job_id = ? AND resume_id = ?", (job_id, resume_id)
        ).fetchone()
        conn.execute(
            "INSERT INTO job_resumes"
            " (job_id, resume_id, file_name, stored_path, origin, status, error, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)"
            " ON CONFLICT(job_id, resume_id) DO UPDATE SET"
            "   file_name = excluded.file_name,"
            "   stored_path = excluded.stored_path,"
            "   origin = excluded.origin,"
            "   status = excluded.status,"
            "   error = '',"
            "   updated_at = excluded.updated_at",
            (job_id, resume_id, file_name, stored_path, origin, status, ts, ts),
        )
    return existed is None


def get_job_resume(db: Database, job_id: str, resume_id: str) -> JobResume | None:
    row = db.query_one(
        "SELECT * FROM job_resumes WHERE job_id = ? AND resume_id = ?", (job_id, resume_id)
    )
    return _row_to_job_resume(row) if row else None


def list_job_resumes(db: Database, job_id: str) -> list[JobResume]:
    rows = db.query(
        "SELECT * FROM job_resumes WHERE job_id = ? ORDER BY created_at DESC, rowid DESC", (job_id,)
    )
    return [_row_to_job_resume(r) for r in rows]


def list_job_resumes_by_status(db: Database, job_id: str, statuses: Sequence[str]) -> list[JobResume]:
    if not statuses:
        return []
    ph = ",".join("?" * len(statuses))
    rows = db.query(
        f"SELECT * FROM job_resumes WHERE job_id = ? AND status IN ({ph})"
        " ORDER BY created_at ASC, rowid ASC",
        [job_id, *statuses],
    )
    return [_row_to_job_resume(r) for r in rows]


def set_resume_status(
    db: Database, job_id: str, resume_id: str, status: ResumeStatus | str, *, error: str = ""
) -> None:
    value = status.value if isinstance(status, ResumeStatus) else status
    db.execute(
        "UPDATE job_resumes SET status = ?, error = ?, updated_at = ? WHERE job_id = ? AND resume_id = ?",
        (value, error, now_iso(), job_id, resume_id),
    )


def remove_job_resume(db: Database, job_id: str, resume_id: str) -> bool:
    with db.tx() as conn:
        cur = conn.execute(
            "DELETE FROM job_resumes WHERE job_id = ? AND resume_id = ?", (job_id, resume_id)
        )
    return cur.rowcount > 0


def reset_stuck_resumes(db: Database) -> int:
    """启动时把滞留在中间态的简历重置为 queued 并返回数量（技术方案 §4.2）。

    没有这一步，进程重启后简历会永久卡在"处理中"——而"让 HR 以为系统看过"
    正是产品方案 §6.1 明令禁止的。
    """
    ph = ",".join("?" * len(PENDING_STATUSES))
    with db.tx() as conn:
        cur = conn.execute(
            f"UPDATE job_resumes SET status = ?, error = '', updated_at = ? WHERE status IN ({ph})",
            [ResumeStatus.QUEUED.value, now_iso(), *PENDING_STATUSES],
        )
    return cur.rowcount


def list_pending_resumes(db: Database) -> list[JobResume]:
    """所有仍处于中间态的简历（跨岗位），供启动时重新入队。"""
    ph = ",".join("?" * len(PENDING_STATUSES))
    rows = db.query(
        f"SELECT * FROM job_resumes WHERE status IN ({ph}) ORDER BY created_at ASC, rowid ASC",
        list(PENDING_STATUSES),
    )
    return [_row_to_job_resume(r) for r in rows]


# ——————————————————— 结论 ———————————————————


def put_result(
    db: Database,
    *,
    job_id: str,
    rubric_id: str,
    result: CandidateResult,
    reason_line: str = "",
) -> None:
    """写入结论。主键含 rubric_id，所以换标准重排是**新增**而非覆盖（技术方案 §4.1）。"""
    db.execute(
        "INSERT OR REPLACE INTO results"
        " (job_id, resume_id, rubric_id, verdict, score, reason_line, payload, hr_decision, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?,"
        "   (SELECT hr_decision FROM results WHERE job_id = ? AND resume_id = ? AND rubric_id = ?),"
        "   ?)",
        (
            job_id,
            result.resume_id,
            rubric_id,
            result.verdict.value,
            float(result.score),
            reason_line,
            result.model_dump_json(),
            job_id,
            result.resume_id,
            rubric_id,
            now_iso(),
        ),
    )


def get_result(db: Database, job_id: str, resume_id: str, rubric_id: str) -> sqlite3.Row | None:
    return db.query_one(
        "SELECT * FROM results WHERE job_id = ? AND resume_id = ? AND rubric_id = ?",
        (job_id, resume_id, rubric_id),
    )


def list_results(db: Database, job_id: str, rubric_id: str) -> list[sqlite3.Row]:
    """按岗位 + 标准版本取结论，排序交给 services 层（避免存储层掺业务规则）。"""
    return db.query(
        "SELECT * FROM results WHERE job_id = ? AND rubric_id = ?", (job_id, rubric_id)
    )


def set_hr_decision(
    db: Database, job_id: str, resume_id: str, rubric_id: str, decision: str | None
) -> bool:
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE results SET hr_decision = ?, updated_at = ?"
            " WHERE job_id = ? AND resume_id = ? AND rubric_id = ?",
            (decision, now_iso(), job_id, resume_id, rubric_id),
        )
    return cur.rowcount > 0


def count_results_by_verdict(db: Database, job_id: str, rubric_id: str) -> dict[str, int]:
    rows = db.query(
        "SELECT verdict, COUNT(*) AS n FROM results WHERE job_id = ? AND rubric_id = ? GROUP BY verdict",
        (job_id, rubric_id),
    )
    return {r["verdict"]: r["n"] for r in rows}


def iter_all_resume_ids(db: Database) -> Iterable[str]:
    for row in db.query("SELECT resume_id FROM resume_docs"):
        yield row["resume_id"]


# ——————————————————————— 改标准提案 ———————————————————————


def put_proposal(
    db: Database,
    *,
    job_id: str,
    utterance: str,
    base_rubric_id: str,
    payload: dict[str, Any],
    impact: dict[str, Any],
) -> ProposalRecord:
    proposal_id = new_id("prop_")
    ts = now_iso()
    db.execute(
        "INSERT INTO rubric_proposals"
        " (proposal_id, job_id, utterance, base_rubric_id, payload, impact, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            proposal_id,
            job_id,
            utterance,
            base_rubric_id,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(impact, ensure_ascii=False),
            ts,
        ),
    )
    return ProposalRecord(
        proposal_id=proposal_id,
        job_id=job_id,
        utterance=utterance,
        base_rubric_id=base_rubric_id,
        payload=payload,
        impact=impact,
        created_at=ts,
    )


def _row_to_proposal(row: sqlite3.Row) -> ProposalRecord:
    return ProposalRecord(
        proposal_id=row["proposal_id"],
        job_id=row["job_id"],
        utterance=row["utterance"],
        base_rubric_id=row["base_rubric_id"],
        payload=json.loads(row["payload"]),
        impact=json.loads(row["impact"]),
        created_at=row["created_at"],
    )


def get_proposal(db: Database, proposal_id: str) -> ProposalRecord | None:
    row = db.query_one("SELECT * FROM rubric_proposals WHERE proposal_id = ?", (proposal_id,))
    return _row_to_proposal(row) if row else None

