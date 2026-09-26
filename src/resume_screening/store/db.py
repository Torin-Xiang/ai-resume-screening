"""SQLite 存储层：连接管理与建表（技术方案 §2.3 / §4）。

约定：
1. **短连接**：每次操作开一个新连接，用完即关。SQLite 建连接开销很小，
   这样天然线程安全（后台 worker 线程与请求线程可同时使用），不需要自己加锁。
2. 外键约束打开，保证岗位删除时关联数据一致。
3. 数据目录由 `APP_DATA_DIR` 决定，默认 `<项目根>/data`——为了将来换机器/部署时
   只搬一个目录（技术方案 §2.2 预留要求）。
4. **日志模式用 TRUNCATE**（实测后定的，见下文与 `JOURNAL_MODE` 处的对照表）。

关于第 4 点——这是本文件最容易被改回去的地方，把依据写清楚。

先排除 WAL。最初的方案是"短连接 + WAL"，想法是取两者的好处，实测发现这两个选择
**互相冲突**：WAL 依赖 `-wal` / `-shm` 两个附属文件，而短连接在每次关闭时会把它们
删掉，下一个连接又得重建。本机实测（同一份代码，只换日志模式，各 15 次取中位数）：

| 日志模式 | 短连接下一次"连上 + 读一张表" | 说明 |
|---|---|---|
| WAL | **75 ms**（D 盘）/ 112 ms（系统临时目录） | 每次重建 `-shm` |
| 回滚日志 | **1.4 ms** | 无附属文件 |

如果把某个连接一直握着不放，WAL 会降到 3.9 ms——慢的不是 WAL 本身，是这个组合。
一次排名请求要读好几张表，75 ms × N 是肉眼可见的卡顿，而**速度就是这个产品的生死线**
（产品方案 §9：一旦要等，产品就退化回跑批）。

于是在回滚日志家族里选（DELETE / TRUNCATE / PERSIST）。上一轮选了 DELETE，
只测了读（1.4 ms，很好），**没测写**——直到一条并发集成测试偶发失败才回头补测，
发现 DELETE 的每次提交要 76 ms，而同样的语义下 TRUNCATE 只要 8 ms（对照表见
`JOURNAL_MODE`）。原因是 DELETE 提交时要 unlink 日志文件（一次删除 + 一次目录 fsync），
TRUNCATE 只是把它截断为 0 字节留着复用。写在这个应用里极其频繁（每份简历每步状态
写一次），所以这一项差别直接影响"上传后多久看到结果"。

结论：TRUNCATE + 默认的 `synchronous=FULL`（不牺牲 crash 安全）+ 短连接。
写与读在这套组合下都很快，且不产生 `-wal` / `-shm`。

剩下的代价是"回滚日志模式下写与读写互斥"，它带来一个必须在代码里处理掉的问题——
并发写会撞锁。见 `tx()` 的说明。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

# 见模块开头第 4 点的实测依据：不要在不改连接策略的情况下换回 WAL。
#
# 为什么是 TRUNCATE 而不是 DELETE（一次写事务，单线程，中位数）：
#
# | 日志模式 | 写事务 | 读连接 | 附属文件 |
# |---|---|---|---|
# | DELETE | 76 ms | 4.2 ms | 无 |
# | **TRUNCATE** | **8.2 ms** | **4.1 ms** | 零长度 `-journal` |
# | PERSIST | 9.6 ms | 11.5 ms | 零长度 `-journal` |
# | WAL | 140 ms | — | `-wal` / `-shm` |
#
# DELETE 慢在提交时要**删除**日志文件：一次 unlink 加一次目录 fsync，在这台机器上
# 就要 70ms。TRUNCATE 只是把它截断成 0 字节，文件留着复用，同样的回滚日志语义、
# 同样的 crash 安全等级（`synchronous` 保持默认的 FULL）、读性能也一致。
# 写在这个应用里发生得非常频繁（每份简历每步状态都写一次），所以这是实打实的差别。
JOURNAL_MODE = "TRUNCATE"

# 写事务的进程内闸门，按数据库文件路径区分（同一个库的多个 Database 实例共用一把）。
# 用 RLock 而不是 Lock：万一有人在 tx() 里又调了一次写（现在没有），
# 同线程重入不该自己把自己卡死；跨线程仍然是互斥的。
_WRITE_LOCKS: dict[str, threading.RLock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _write_lock_for(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _WRITE_LOCKS[key] = lock
        return lock

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

-- 自然语言改标准的提案（技术方案 §7）。
-- 落库而不是让前端把清单原样传回来，原因有二：
-- 1.「部分采纳」要按序号取子集，序号必须指向服务端可复现的那份清单；
-- 2. 提案是"谁在什么标准基础上提了什么要求"的记录，标准可追溯不只指版本号，
--    还包括"这一版是怎么改出来的"（产品方案 §6.4）。
CREATE TABLE IF NOT EXISTS rubric_proposals (
  proposal_id    TEXT PRIMARY KEY,
  job_id         TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  utterance      TEXT NOT NULL,
  base_rubric_id TEXT NOT NULL,
  payload        TEXT NOT NULL,
  impact         TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_job ON rubric_proposals(job_id, created_at);
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
        # **每个连接都要设**：TRUNCATE / PERSIST / MEMORY / OFF 这类回滚日志模式
        # 不是持久化属性（只有 WAL 会写进文件头）。只在建库时设一次的话，
        # 之后每个新连接都会静默退回默认的 DELETE —— 而 DELETE 每次提交要
        # unlink 日志文件，一次写事务 76 ms 对 8 ms。这个坑很隐蔽：
        # 设置语句"成功了"，读 `PRAGMA journal_mode` 在当时那个连接上也对，
        # 只有换一个连接才看得出来。
        conn.execute(f"PRAGMA journal_mode={JOURNAL_MODE}")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：进程内串行 + BEGIN IMMEDIATE，正常结束提交，异常回滚。

        这是本文件第二个容易被改回去的地方，把依据写清楚。

        **为什么用一把进程内锁把写排成队**：SQLite 在回滚日志模式下写与读写互斥，
        两个连接同时想写时，后到的那个走 busy handler 退避重试。实测这把锁的价值
        不在于"更快"（每次写的 8ms 主要是 fsync，省不掉），而在于**把问题从
        "概率性报错"变成"排队"**：进程内的写永远不会到达 SQLite 的锁协商，
        于是 "database is locked" 这一类偶发失败在本进程里结构性地不可能发生。
        这个应用有 worker 线程池 + 请求线程同时在写（每份简历每步状态写一次），
        正是这类竞争的高发场景。

        **为什么仍然保留 BEGIN IMMEDIATE**：进程内锁只覆盖本进程。
        如果将来变成多进程（多 worker 部署），这层锁就失效了，届时起作用的是
        IMMEDIATE + `timeout`。DEFERRED 下"先读后写"的事务在升级锁时会**立刻**
        报 "database is locked"，连 timeout 都不等（等下去会死锁），
        那种偶发失败比慢更难查。两件事不冲突：锁负责确定，IMMEDIATE 负责兜底。

        **使用约定**：tx() 里只做短小的语句组，不要调模型、不要读文件。
        锁是跨线程的，在事务里做慢事会挡住所有写入。
        """
        with _write_lock_for(self.path):
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
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
            # connect() 里已经设过日志模式；这里再设一次是为了把**已存在的旧库**
            # 从 WAL 转换过来（WAL 是写进文件头的持久属性，得主动改）。
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
