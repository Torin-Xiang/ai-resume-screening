"""SQLite 存储层：连接管理与建表（技术方案 §2.3 / §4）。

约定：
1. **短连接**：每次操作开一个新连接，用完即关。SQLite 建连接开销很小，
   这样天然线程安全（后台 worker 线程与请求线程可同时使用），不需要自己加锁。
2. 外键约束打开，保证岗位删除时关联数据一致。
3. 数据目录由 `APP_DATA_DIR` 决定，默认 `<项目根>/data`——为了将来换机器/部署时
   只搬一个目录（技术方案 §2.2 预留要求）。
4. **日志模式用 DELETE 而不是 WAL**（实测后改的，见下）。

关于第 4 点——这是本文件最容易被改回去的地方，把依据写清楚：

最初的方案是"短连接 + WAL"，想法是取两者的好处。实测发现这两个选择**互相冲突**：
WAL 模式依赖 `-wal` / `-shm` 两个附属文件，而短连接在每次关闭时会把它们删掉，
下一个连接又得重建。本机实测（同一份代码，只换日志模式，各 15 次取中位数）：

| 日志模式 | 短连接下一次"连上 + 读一张表" | 说明 |
|---|---|---|
| WAL | **75 ms**（D 盘）/ 112 ms（系统临时目录） | 每次重建 `-shm` |
| DELETE | **1.4 ms** | 无附属文件 |

如果把某个连接一直握着不放，WAL 会降到 3.9 ms——也就是说慢的不是 WAL 本身，
而是"WAL + 短连接"这个组合。一次排名请求要读好几张表，75 ms × N 是肉眼可见的卡顿，
而**速度就是这个产品的生死线**（产品方案 §9：一旦要等，产品就退化回跑批）。

取舍：DELETE 模式下写会短暂阻塞读。本项目已确认是**单机单人**使用（产品方案 §10），
写入是零星的（上传/改标准），且 `timeout=30.0` 会让并发写入排队而不是报错。
用 WAL 换来的那点并发，在这个场景里买不到东西，却要每次操作都付一遍重建成本。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

# 见模块开头第 4 点的实测依据：不要在不改连接策略的情况下换回 WAL。
JOURNAL_MODE = "DELETE"

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
            # 显式设置而不是沿用默认值：默认值会随发行版变化，而这个选择有实测依据
            # （见模块开头）。已存在的库也会在这里被转换过来。
            conn.execute(f"PRAGMA journal_mode={JOURNAL_MODE}")
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
