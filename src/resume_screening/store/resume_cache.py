"""解析/提取缓存的 SQLite 适配器。

v0.1 的 `core.cache.ResumeCache` 把结果写成 JSON 文件；进入服务化形态后，
数据统一落在 SQLite，备份与搬机器只需一个文件（技术方案 §2.3）。

**关键点：只实现 `hit` / `put`，`core.extractor` 不用改。**
这正是"新增能力 = 加适配器，不动核心"（方案 §4.1）在缓存这一层上的体现。

⚠️ **但"不动核心"的前提是两个实现随时保持同一套接口。** 只给文件版加参数、
忘了这里，就会出这种事：服务端注入的是本适配器，`extract_resume` 带着新参数
调用 → `TypeError` → 异常被后台任务吞掉 → 上传后一份简历都出不来，
而单跑 CLI 完全正常（CLI 用的是文件版）。改接口时两处必须一起动，
`tests/test_m2_extraction.py` 里有一条契约测试盯着这件事。
"""

from __future__ import annotations

from ..core.models import ResumeDocument
from . import repo
from .db import Database


class SqliteResumeCache:
    """与 `ResumeCache` 接口对齐，供 `extract_resume(cache=...)` 注入。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def hit(self, resume_id: str, *, expect_extractor: str | None = None) -> ResumeDocument | None:
        """读缓存；`expect_extractor` 与文档记录的不一致时视为未命中。

        语义与文件版 `core.cache.ResumeCache.hit` 完全一致——两处必须同时改。
        """
        doc = repo.get_resume_doc(self._db, resume_id)
        if doc is None:
            return None
        if expect_extractor is not None and doc.extractor != expect_extractor:
            return None
        return doc

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
