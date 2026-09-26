"""接收入库：上传落盘 → 关联岗位（技术方案 §6 的 ①② 步）。

**重要：这里只做落盘与建关联，不做解析。** 解析在后台 worker 中进行。
把这两件事分开，是为了让上传请求立刻返回——否则一份简历十几秒、
十份就超时，用户看到转圈然后失败，正好退化成产品方案 §1 批评的那种体验。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..core.ids import resume_id_of
from ..store import repo
from ..store.db import Database
from ..store.entities import ResumeStatus

SUPPORTED_SUFFIXES: tuple[str, ...] = (".pdf",)
"""M6 只支持 PDF（与 v0.1 一致）。docx 等在 M10 之后按"注册一个解析器"的方式扩展。"""


class UnsupportedFormat(ValueError):
    """文件格式不受支持。"""


@dataclass
class IngestedResume:
    resume_id: str
    file_name: str
    stored_path: str
    is_new: bool
    origin: str = "upload"


def store_bytes(db: Database, *, file_name: str, content: bytes) -> tuple[str, Path]:
    """把上传内容按**内容 hash** 落盘，返回 (resume_id, 存储路径)。

    用 hash 命名而不是原始文件名，有两个好处：同名不同内容不会互相覆盖；
    同内容重复上传天然合并，不重复计费（产品方案 §2.2 允许 HR 重复投递，
    但系统不该为同一份文件算两次）。
    """
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        allowed = "、".join(SUPPORTED_SUFFIXES)
        raise UnsupportedFormat(f"暂不支持 {suffix or '无扩展名'} 格式，当前仅支持：{allowed}")

    uploads = db.uploads_dir
    tmp = uploads / f".upload-{uuid4().hex}{suffix}"
    tmp.write_bytes(content)
    try:
        resume_id = resume_id_of(tmp)
        final = uploads / f"{resume_id}{suffix}"
        if final.exists():
            tmp.unlink()
        else:
            tmp.replace(final)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return resume_id, final


def ingest_upload(
    db: Database,
    *,
    job_id: str,
    file_name: str,
    content: bytes,
    origin: str = "upload",
) -> IngestedResume:
    """落盘 + 关联到岗位，状态置为 queued。调用方随后负责入队。"""
    resume_id, path = store_bytes(db, file_name=file_name, content=content)
    is_new = repo.upsert_job_resume(
        db,
        job_id=job_id,
        resume_id=resume_id,
        file_name=Path(file_name).name,
        stored_path=str(path),
        origin=origin,
        status=ResumeStatus.QUEUED.value,
    )
    return IngestedResume(
        resume_id=resume_id,
        file_name=Path(file_name).name,
        stored_path=str(path),
        is_new=is_new,
        origin=origin,
    )


def ingest_path(
    db: Database, *, job_id: str, file_path: str | Path, origin: str = "upload"
) -> IngestedResume:
    """从本地已有文件入库（CLI 或批量导入用）。文件会被复制进数据目录。"""
    src = Path(file_path)
    if not src.is_file():
        raise FileNotFoundError(f"文件不存在：{src}")
    return ingest_upload(db, job_id=job_id, file_name=src.name, content=src.read_bytes(), origin=origin)
