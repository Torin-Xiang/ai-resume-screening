"""存储层的日志模式与并发写行为。

这个文件存在的理由是两个实测出来的坑，都值得单独钉住。

**坑一：短连接不能用 WAL，回滚日志家族里要选 TRUNCATE。**
WAL 依赖 `-wal` / `-shm` 两个附属文件，而短连接每次关闭都会删掉它们、下个连接重建。
同一份代码只换日志模式（各 15 次取中位数，读一次"连上 + 读表"）：

| 日志模式 | 读 | 说明 |
|---|---|---|
| WAL | 75 ms（D 盘）/ 112 ms（临时目录） | 每次重建 `-shm` |
| 回滚日志 | 1.4 ms | 无附属文件 |

慢的不是 WAL，是"WAL + 短连接"这个组合：把连接一直握着，WAL 会掉到 3.9 ms。

**坑一的续集：回滚日志家族里还要挑。** 第一轮只测了读就选了 DELETE，写没测——
直到一条并发集成测试偶发失败才回头补测（单线程一次写事务的中位数）：

| 日志模式 | 写事务 | 读 | 附属文件 |
|---|---|---|---|
| DELETE | 76 ms | 4.2 ms | 无 |
| **TRUNCATE** | **8.2 ms** | **4.1 ms** | 零长度 `-journal` |
| PERSIST | 9.6 ms | 11.5 ms | 零长度 `-journal` |
| WAL | 140 ms | — | `-wal` / `-shm` |

DELETE 慢在提交时要 unlink 日志文件（一次删除 + 一次目录 fsync）。TRUNCATE 只是
把它截断为 0 字节留着复用。写在这个应用里极其频繁（每份简历每步状态写一次），
所以这一项直接影响"上传后多久看到结果"。

**坑二：写事务必须用 BEGIN IMMEDIATE，且进程内要有一把写锁。**
默认的 DEFERRED 事务以读身份开始，到第一条写语句才升级锁——这一刻若别的连接握着
写锁，SQLite 会**立刻**报 "database is locked"，`timeout=30` 完全无效（等下去会死锁）。
表现就是集成测试偶发失败（十几轮出现一次），单跑永远复现不出来。

**这里刻意不写"耗时必须小于 X 毫秒"的断言**：那种断言测的是"这台机器有多快"，
不是"设计对不对"，机器一忙就会误报（本项目已经因为类似的绝对阈值误报过一次）。
一律改用确定性的事实来钉——日志模式是哪个、附属文件在不在、大小是不是 0、
并发下有没有报错、写入有没有丢。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from resume_screening.store.db import JOURNAL_MODE, Database
from resume_screening.store import repo


def _journal_mode(db: Database) -> str:
    """读**经由我们的连接工厂**的实际日志模式。

    刻意不走裸 `sqlite3.connect`：TRUNCATE 这类回滚日志模式不是持久化属性
    （只有 WAL 写进文件头），裸连接看到的是默认值 DELETE，那正是我们要防的状态。
    """
    conn = db.connect()
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


def test_new_database_uses_truncate_journal(tmp_path: Path) -> None:
    db = Database(tmp_path / "d").init_schema()
    assert _journal_mode(db) == "truncate" == JOURNAL_MODE.lower()


def test_every_new_connection_gets_the_journal_mode(tmp_path: Path) -> None:
    """**每个**新连接都要是 TRUNCATE，不能只在建库时设一次。

    这是那个写性能坑最阴的地方：`PRAGMA journal_mode=TRUNCATE` 设置时返回
    "truncate"，在当时那个连接上读回来也是 "truncate"，看起来完全生效了——
    但它不是持久化属性，下一个连接会静默退回 DELETE（每次提交 unlink 日志文件，
    76 ms 对 8 ms）。只有"换一个连接再读"才能暴露，所以这条断言连开三次连接。
    """
    db = Database(tmp_path / "d").init_schema()
    repo.create_job(db, title="岗位", jd_text="JD")

    modes = [_journal_mode(db) for _ in range(3)]
    assert modes == ["truncate"] * 3, f"新连接没有沿用日志模式，实际依次为 {modes}"


def test_short_connections_leave_no_wal_sidecar(tmp_path: Path) -> None:
    """短连接跑完之后不应残留 `-wal` / `-shm`。

    这是那个读性能坑的**确定性**表现：只要出现这两个文件，
    就说明每个连接都在重建共享内存，单次操作会慢几十倍。
    """
    db = Database(tmp_path / "d").init_schema()
    repo.create_job(db, title="岗位", jd_text="JD")
    repo.create_job(db, title="岗位2", jd_text="JD")
    db.query("SELECT 1")

    leftovers = [p.name for p in db.data_dir.iterdir() if p.name.endswith(("-wal", "-shm"))]
    assert leftovers == [], f"短连接模式下不应产生 WAL 附属文件，实际残留：{leftovers}"


def test_journal_sidecar_never_accumulates_log(tmp_path: Path) -> None:
    """TRUNCATE 会留下一个 `-journal` 文件，但它必须是 0 字节。

    非 0 就说明有未提交的事务残留——那要么是崩溃留下的，要么是根本没提交成功。
    这条断言保住了"选 TRUNCATE 只是换了种丢日志的方式"这个前提：
    拿它换写性能可以，但不能连"日志不累积"一起换掉。
    """
    db = Database(tmp_path / "d").init_schema()
    repo.create_job(db, title="岗位", jd_text="JD")
    repo.create_job(db, title="岗位2", jd_text="JD")

    journal = db.path.with_name(db.path.name + "-journal")
    assert journal.is_file(), "TRUNCATE 下应当有一个可复用的日志文件"
    assert journal.stat().st_size == 0, f"日志文件应在提交后被截断，实际 {journal.stat().st_size} 字节"


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
    assert _journal_mode(db) == "truncate"


def test_foreign_keys_stay_on(tmp_path: Path) -> None:
    """换日志模式不该顺手把外键约束丢掉——岗位删除要靠它级联清理。"""
    db = Database(tmp_path / "d").init_schema()
    with db.read() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# ——————————————————— 并发写（坑二）———————————————————


def test_write_transaction_takes_the_lock_at_entry(tmp_path: Path) -> None:
    """写事务一进来就该持有写锁，而不是等到第一条写语句。

    这是 BEGIN IMMEDIATE 与 DEFERRED 唯一的可观察差异，也正是"先读后写"会不会撞上
    database is locked 的分水岭。用一个旁路连接当探针：事务刚开、还什么都没写时，
    别的连接就应该已经写不进去了。DEFERRED 下这个探针能写成功——那正说明锁还没拿。
    """
    db = Database(tmp_path / "d").init_schema()
    with db.tx() as conn:
        conn.execute("CREATE TABLE probe (x INTEGER)")

    with db.tx() as conn:
        prober = sqlite3.connect(db.path, timeout=0.2)
        try:
            with pytest.raises(sqlite3.OperationalError):
                prober.execute("INSERT INTO probe (x) VALUES (1)")
        finally:
            prober.close()  # 先放掉探针持有的读锁，否则自己的提交也会被挡住
        conn.execute("INSERT INTO probe (x) VALUES (2)")

    assert [row["x"] for row in db.query("SELECT x FROM probe")] == [2]


def test_concurrent_read_then_write_never_raises_database_locked(tmp_path: Path) -> None:
    """并发跑"先读后写"的事务，一次都不许出 database is locked。

    这是那个偶发集成测试失败的**可控复现**：worker 线程在写简历状态的同时，
    上传请求也在写同一张表，形状正是"先 SELECT 再 INSERT"。
    用 barrier 让所有线程同时起步制造真争抢，比 sleep 抢时间可靠得多——
    靠 sleep 的并发测试在慢机器上会随机通过，那种红会教人忽略它。

    这里不设耗时上限：那测的是机器有多快。真要卡死的话，
    `join(timeout=60)` 之后的写入数断言会先失败。
    """
    db = Database(tmp_path / "d").init_schema()
    with db.tx() as conn:
        conn.execute("CREATE TABLE scratch (k TEXT PRIMARY KEY, v INTEGER NOT NULL)")

    thread_count, rounds = 8, 40
    barrier = threading.Barrier(thread_count)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        try:
            barrier.wait(timeout=10)
            for i in range(rounds):
                # 先读后写：DEFERRED 下这正是在中途升级锁、最容易被撞的形状
                with db.tx() as conn:
                    conn.execute("SELECT COUNT(*) FROM scratch").fetchone()
                    conn.execute(
                        "INSERT INTO scratch (k, v) VALUES (?, ?)"
                        " ON CONFLICT(k) DO UPDATE SET v = v + 1",
                        (f"k{n}", i),
                    )
        except BaseException as exc:  # noqa: BLE001 - 收集所有线程的失败统一断言
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"并发读改写出现 {len(errors)} 次失败，首个：{errors[0]!r}"
    # 每个人都写成了：确认"排队"没有变成"静默丢写"
    rows = db.query("SELECT k, v FROM scratch ORDER BY k")
    assert len(rows) == thread_count, "有线程没能写入，可能是等锁超时后静默失败"
    assert all(row["v"] == rounds - 1 for row in rows), "每个键都该被自己的最后一轮写到"
