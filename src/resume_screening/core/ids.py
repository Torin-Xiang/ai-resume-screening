"""标识与哈希：resume_id 基于文件内容，run_id 每次运行唯一（方案 §6.2）。"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path


def file_content_hash(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    """按文件**内容**计算 sha256。内容不变 → resume_id 不变 → 不重复解析。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def resume_id_of(path: str | Path) -> str:
    """resume_id = 内容 hash 前 16 位（足够区分，且便于人眼核对）。"""
    return file_content_hash(path)[:16]


def new_run_id() -> str:
    """run_id：每次筛选运行唯一，形如 20260926T011200Z-a1b2c3。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{uuid.uuid4().hex[:6]}"


def new_id(prefix: str = "") -> str:
    """通用短 id：`prefix` + 12 位十六进制。

    用于岗位（`job_`）、标准版本（`rub_`）等需要稳定标识的实体。
    与 `resume_id`（内容 hash，可复现）不同，这类 id 只要求唯一。
    """
    return f"{prefix}{uuid.uuid4().hex[:12]}"
