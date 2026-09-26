"""后台处理链路：上传即处理（产品方案 §6.1 / 技术方案 §6）。

设计要点：

1. **并发靠消费者数量**，不额外加信号量。默认 5 路，即最多 5 个模型调用同时在飞，
   避免触发服务商限流。并发度可配置，具体档位由实测确定（技术方案 §10）。
2. **阻塞调用下放到线程池**（`asyncio.to_thread`），因为解析与模型调用都是同步阻塞的；
   这样事件循环不被占住，SSE 推送才能实时。
3. **异常一律转成状态，绝不抛出**。任何一份简历失败都不能影响其他简历，
   也不能让 HR 看到一个没有状态的行。
4. **`needs_manual` 必须单独处理并且推送事件**——解析不达标时不送模型，
   但也绝不允许静默跳过（产品方案 §6.1）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Protocol

from ..core.llm import LLMClient
from ..core.pipeline import (
    STAGE_EXTRACTING,
    STAGE_PARSING,
    STAGE_SCORING,
    process_one,
    process_text,
)
from ..core.ports import ResumeParser
from ..store import repo
from ..store.db import Database
from ..store.entities import ResumeStatus
from . import jobs as jobs_service
from .events import EventBus
from .ingest import ORIGIN_PASTE
from .ranking import one_line_reason

STAGE_TO_STATUS: dict[str, ResumeStatus] = {
    STAGE_PARSING: ResumeStatus.PARSING,
    STAGE_EXTRACTING: ResumeStatus.EXTRACTING,
    STAGE_SCORING: ResumeStatus.SCORING,
}

DEFAULT_CONCURRENCY = 5
MAX_ERROR_CHARS = 500


class ResumeCacheLike(Protocol):
    """解析缓存的最小接口（见 store.resume_cache.SqliteResumeCache）。"""

    def hit(self, resume_id: str) -> Any: ...
    def put(self, doc: Any) -> Any: ...


class ScreeningWorker:
    """后台简历处理队列。"""

    def __init__(
        self,
        *,
        db: Database,
        parser: ResumeParser,
        llm: LLMClient,
        bus: EventBus,
        cache: ResumeCacheLike | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._db = db
        self._parser = parser
        self._llm = llm
        self._bus = bus
        self._cache = cache
        self._concurrency = max(1, int(concurrency))
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._consumers: list[asyncio.Task] = []
        self._inflight: set[tuple[str, str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # —————————————————— 生命周期 ——————————————————

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._consumers = [
            asyncio.create_task(self._consume(i), name=f"screen-worker-{i}")
            for i in range(self._concurrency)
        ]

    async def stop(self) -> None:
        for task in self._consumers:
            task.cancel()
        for task in self._consumers:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._consumers.clear()

    # —————————————————— 入队与等待 ——————————————————

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def pending(self) -> int:
        """排队中 + 处理中的数量。"""
        return self._queue.qsize() + len(self._inflight)

    def enqueue(self, job_id: str, resume_id: str) -> None:
        """入队。必须在事件循环内调用（web 路由与测试均满足）。"""
        self._queue.put_nowait((job_id, resume_id))

    def requeue_stuck(self) -> int:
        """启动时把上次进程留下的中间态任务重新排队（技术方案 §4.2）。

        顺序很重要：先取出列表（此时还是中间态），再重置状态，最后入队。
        """
        stuck = repo.list_pending_resumes(self._db)
        if not stuck:
            return 0
        repo.reset_stuck_resumes(self._db)
        for record in stuck:
            self.enqueue(record.job_id, record.resume_id)
        return len(stuck)

    async def drain(self, *, timeout: float = 120.0) -> bool:
        """等待队列清空。超时返回 False（测试与优雅关停用）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.pending:
            if loop.time() > deadline:
                return False
            await asyncio.sleep(0.02)
        return True

    # —————————————————— 消费 ——————————————————

    async def _consume(self, _idx: int) -> None:
        while True:
            job_id, resume_id = await self._queue.get()
            key = (job_id, resume_id)
            if key in self._inflight:
                # 重复入队（如同一文件被连点两次上传）只处理一次
                self._queue.task_done()
                continue
            self._inflight.add(key)
            try:
                await self._process(job_id, resume_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都要落到状态上
                self._fail(job_id, resume_id, self._describe(exc))
            finally:
                self._inflight.discard(key)
                self._queue.task_done()

    async def _process(self, job_id: str, resume_id: str) -> None:
        record = repo.get_job_resume(self._db, job_id, resume_id)
        if record is None:
            return

        try:
            rubric, rubric_info = jobs_service.require_rubric(self._db, job_id)
        except jobs_service.RubricNotSet as exc:
            self._fail(job_id, resume_id, str(exc))
            return

        emit = self._stage_emitter(job_id, resume_id)
        try:
            doc, result = await asyncio.to_thread(
                self._run_one,
                record=record,
                rubric=rubric,
                emit=emit,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(job_id, resume_id, self._describe(exc))
            return

        if result is None:
            reason = "；".join(doc.parse_quality.reasons) or "未提取到可用文本"
            repo.set_resume_status(
                self._db, job_id, resume_id, ResumeStatus.NEEDS_MANUAL, error=reason
            )
            self._publish_status(job_id, resume_id, ResumeStatus.NEEDS_MANUAL, reason)
            self._schedule_ranking(job_id)
            return

        # 存储时文件按内容 hash 命名（天然去重），但给 HR 看的必须是上传时的原名。
        # 解析器只能看到存储路径，所以在这里换回原名——这是服务层才知道的信息。
        result.file_name = record.file_name

        repo.put_result(
            self._db,
            job_id=job_id,
            rubric_id=rubric_info.rubric_id,
            result=result,
            reason_line=one_line_reason(result),
        )
        repo.set_resume_status(self._db, job_id, resume_id, ResumeStatus.DONE)
        self._publish_status(job_id, resume_id, ResumeStatus.DONE)
        self._schedule_ranking(job_id)

    def _run_one(self, *, record, rubric, emit: Callable[[str], None]):
        """按来源分派到对应的处理链路。

        「上传」读文件走解析器；「粘贴」直接拿文本（产品方案 §6.1 的兜底）。
        分派只在这一处，两条链路的下游（提取、评分、缓存、结论）完全相同——
        兜底路径不应该是一条"简化版"链路，否则兜底出来的结论就和正常结论不可比。
        """
        if record.origin == ORIGIN_PASTE:
            text = Path(record.stored_path).read_text(encoding="utf-8")
            return process_text(
                text=text,
                resume_id=record.resume_id,
                file_name=record.file_name,
                rubric=rubric,
                llm=self._llm,
                cache=self._cache,
                source_path=record.stored_path,
                on_stage=emit,
            )
        return process_one(
            file_path=record.stored_path,
            rubric=rubric,
            parser=self._parser,
            llm=self._llm,
            cache=self._cache,
            on_stage=emit,
        )

    # —————————————————— 事件 ——————————————————

    def _stage_emitter(self, job_id: str, resume_id: str) -> Callable[[str], None]:
        """返回一个可在线程池中安全调用的阶段回调。

        `process_one` 运行在 `asyncio.to_thread` 的线程里，而 `EventBus` 基于
        `asyncio.Queue`——**不能跨线程直接调用**。这里用 `call_soon_threadsafe`
        把事件投递回事件循环，这是本模块唯一需要注意的并发细节。
        """

        def emit(stage: str) -> None:
            status = STAGE_TO_STATUS.get(stage)
            if status is None:
                return
            repo.set_resume_status(self._db, job_id, resume_id, status)
            loop = self._loop
            payload = {"resume_id": resume_id, "status": status.value, "error": ""}
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._bus.publish, job_id, "resume.status", payload)

        return emit

    def _publish_status(
        self, job_id: str, resume_id: str, status: ResumeStatus, error: str = ""
    ) -> None:
        self._bus.publish(
            job_id,
            "resume.status",
            {"resume_id": resume_id, "status": status.value, "error": error},
        )

    def _schedule_ranking(self, job_id: str) -> None:
        def produce() -> dict[str, Any]:
            info = jobs_service.current_rubric_info(self._db, job_id)
            counts = (
                repo.count_results_by_verdict(self._db, job_id, info.rubric_id) if info else {}
            )
            return {"job_id": job_id, "counts": counts, "total": sum(counts.values())}

        self._bus.schedule_ranking(job_id, produce)

    def _fail(self, job_id: str, resume_id: str, error: str) -> None:
        message = error[:MAX_ERROR_CHARS]
        repo.set_resume_status(self._db, job_id, resume_id, ResumeStatus.FAILED, error=message)
        self._publish_status(job_id, resume_id, ResumeStatus.FAILED, message)
        self._schedule_ranking(job_id)

    @staticmethod
    def _describe(exc: BaseException) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        return f"{exc.__class__.__name__}：{text}"[:MAX_ERROR_CHARS]
