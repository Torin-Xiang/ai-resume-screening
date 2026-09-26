"""SQLite 存储层：连接管理与建表（技术方案 §2.3 / §4）。

约定：
1. **短连接**：每次操作开一个新连接，用完即关。SQLite 建连接开销很小，
   这样天然线程安全（后台 worker 线程与请求线程可同时使用），不需要自己加锁。
2. 建库时开启 WAL，提升读写并发；外键约束打开，保证岗位删除时关联数据一致。
3. 数据目录由 `APP_DATA_DIR` 决定，默认 `<项目根>/data`——为了将来换机器/部署时
   只搬一个目录（技术方案 §2.2 预留要求）。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id      TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  jd_text     TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'open',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rubric_versions (
  rubric_id   TEXT PRIMARY KEY,
  job_id      TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  version     TEXT NOT NULL,
  payload     TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rubric_job ON rubric_versions(job_id, created_at);

CREATE TABLE IF NOT EXISTS resume_docs (
  resume_id    TEXT PRIMARY KEY,
  doc_payload  TEXT NOT NULL,
  parse_status TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_resumes (
  job_id      TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  resume_id   TEXT NOT NULL,
  file_name   TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  origin      TEXT NOT NULL DEFAULT 'upload',
  status      TEXT NOT NULL,
  error       TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (job_id, resume_id)
);
CREATE INDEX IF NOT EXISTS idx_job_resumes_status ON job_resumes(job_id, status);

CREATE TABLE IF NOT EXISTS results (
  job_id      TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  resume_id   TEXT NOT NULL,
  rubric_id   TEXT NOT NULL,
  verdict     TEXT NOT NULL,
  score       REAL NOT NULL DEFAULT 0,
  reason_line TEXT NOT NULL DEFAULT '',
  payload     TEXT NOT NULL,
  hr_decision TEXT,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (job_id, resume_id, rubric_id)
);
CREATE INDEX IF NOT EXISTS idx_results_lookup ON results(job_id, rubric_id);
"""


def resolve_data_dir(data_dir: str | Path | None = None) -> Path:
    """数据目录：显式参数 > 环境变量 APP_DATA_DIR > <cwd>/data。

    目录不存在时创建。
    """
    if data_dir is not None:
        d = Path(data_dir)
    elif os.environ.get("APP_DATA_DIR"):
        d = Path(os.environ["APP_DATA_DIR"])
    else:
        d = Path.cwd() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Database:
    """SQLite 连接工厂。本身不持有长连接。"""

    def __init__(self, data_dir: str | Path | None = None, *, db_name: str = "app.db") -> None:
        self.data_dir = resolve_data_dir(data_dir)
        self.path = self.data_dir / db_name

    @property
    def uploads_dir(self) -> Path:
        d = self.data_dir / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def cache_dir(self) -> Path:
        d = self.data_dir / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：进入即开 BEGIN，正常结束提交，异常回滚。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - 回滚失败时保留原始异常
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """只读访问：不显式开事务，出错即关连接。"""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> "Database":
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA_SQL)
        finally:
            conn.close()
        return self

    # —— 便捷读写（供 services 使用；复杂逻辑请用 tx()/read()）——

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self.read() as conn:
            return list(conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.tx() as conn:
            conn.execute(sql, params)
