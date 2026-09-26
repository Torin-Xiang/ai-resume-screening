"""解析/提取缓存的 SQLite 适配器。

v0.1 的 `core.cache.ResumeCache` 把结果写成 JSON 文件；进入服务化形态后，
数据统一落在 SQLite，备份与搬机器只需一个文件（技术方案 §2.3）。

**关键点：这里只实现 `hit` / `put` 两个方法，`core.extractor` 一行不用改。**
这正是"新增能力 = 加适配器，不动核心"（方案 §4.1）在缓存这一层上的体现。
"""

from __future__ import annotations

from ..core.models import ResumeDocument
from . import repo
from .db import Database


class SqliteResumeCache:
    """与 `ResumeCache` 接口对齐，供 `extract_resume(cache=...)` 注入。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def hit(self, resume_id: str) -> ResumeDocument | None:
        return repo.get_resume_doc(self._db, resume_id)

    def put(self, doc: ResumeDocument) -> None:
        """写入缓存。

        注意：与文件版不同，本实现没有"文件路径"可返回，因此返回 None。
        `extract_resume` 不使用 `put` 的返回值，故安全；若将来有调用方依赖它，
        测试会立刻暴露。
        """
        repo.put_resume_doc(self._db, doc)

    def __len__(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM resume_docs")
        return int(row["n"]) if row else 0
