"""存储层日志模式：短连接必须配 DELETE，不能配 WAL。

这个文件存在的理由是一个实测出来的坑，值得单独钉住：

最初选了"短连接 + WAL"，想法是同时拿到线程安全与读写并发。实测发现两者**互相冲突**——
WAL 依赖 `-wal` / `-shm` 两个附属文件，而短连接每次关闭都会删掉它们，下个连接重建。
同一份代码只换日志模式，各 15 次取中位数：

| 日志模式 | 一次"连上 + 读表" | 一次 `/api/jobs` 请求 |
|---|---|---|
| WAL | 75 ms（D 盘） | 119 ms |
| DELETE | 1.4 ms | 5 ms |

慢的不是 WAL，是"WAL + 短连接"这个组合：把某个连接一直握着不放，WAL 会掉到 3.9 ms。

**这里刻意不写"耗时必须小于 X 毫秒"的断言**：那种断言测的是"这台机器有多快"，
不是"设计对不对"，机器一忙就会误报（本项目已经因为类似的绝对阈值误报过一次）。
改用确定性的事实来钉——附属文件在不在、日志模式是不是预期的那个。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from resume_screening.store.db import JOURNAL_MODE, Database
from resume_screening.store import repo


def _journal_mode(db: Database) -> str:
    conn = sqlite3.connect(db.path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


def test_new_database_uses_delete_journal(tmp_path: Path) -> None:
    db = Database(tmp_path / "d").init_schema()
    assert _journal_mode(db) == "delete" == JOURNAL_MODE.lower()


def test_short_connections_leave_no_wal_sidecar(tmp_path: Path) -> None:
    """短连接跑完之后不应残留 `-wal` / `-shm`。

    这是那个性能坑的**确定性**表现：只要出现这两个文件，
    就说明每个连接都在重建共享内存，单次操作会慢几十倍。
    """
    db = Database(tmp_path / "d").init_schema()
    repo.create_job(db, title="岗位", jd_text="JD")
    repo.create_job(db, title="岗位2", jd_text="JD")
    db.query("SELECT 1")

    leftovers = [p.name for p in db.data_dir.iterdir() if p.name.endswith(("-wal", "-shm"))]
    assert leftovers == [], f"短连接模式下不应产生 WAL 附属文件，实际残留：{leftovers}"


def test_init_schema_converts_existing_wal_database(tmp_path: Path) -> None:
    """已经用 WAL 建过的库也要被转换过来，否则老数据目录会一直是慢的。"""
    data_dir = tmp_path / "d"
    data_dir.mkdir(parents=True)
    legacy = data_dir / "app.db"
    conn = sqlite3.connect(legacy)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()

    db = Database(data_dir).init_schema()
    assert _journal_mode(db) == "delete"


def test_foreign_keys_stay_on(tmp_path: Path) -> None:
    """换日志模式不该顺手把外键约束丢掉——岗位删除要靠它级联清理。"""
    db = Database(tmp_path / "d").init_schema()
    with db.read() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
