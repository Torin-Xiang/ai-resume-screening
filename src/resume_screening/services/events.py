"""进程内事件总线：后台 worker 发布，SSE 端点订阅（技术方案 §5.2）。

为什么用 SSE 而不是 WebSocket：数据流是单向的（服务端 → 浏览器），
SSE 基于普通 HTTP、自动重连、实现与调试都更简单，后端也不需维护双向连接状态。

关于并发：worker 是 asyncio 任务，阻塞的解析与模型调用通过 `asyncio.to_thread`
下放到线程池，因此 publish 始终发生在事件循环内，不需要跨线程投递。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Callable, Iterator

RANKING_DEBOUNCE_SECONDS = 0.2
"""排名推送的合并窗口。批量上传时每份都推会刷屏，合并后只推一次。"""

MAX_QUEUE_SIZE = 256


class EventBus:
    """岗位维度的发布订阅。订阅者慢时丢事件，绝不阻塞后台处理。"""

    def __init__(self, *, ranking_delay: float = RANKING_DEBOUNCE_SECONDS) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._ranking_delay = ranking_delay
        self._pending_rankings: dict[str, Callable[[], dict[str, Any]]] = {}
        self._flusher: asyncio.Task | None = None

    # —— 生命周期 ——

    def start(self) -> None:
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._flusher is not None:
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                pass
            self._flusher = None
        self._pending_rankings.clear()

    # —— 订阅 / 发布 ——

    @contextmanager
    def subscribe(self, job_id: str) -> Iterator[asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._subs[job_id].add(q)
        try:
            yield q
        finally:
            self._subs[job_id].discard(q)

    def publish(self, job_id: str, event: str, data: dict[str, Any]) -> None:
        for q in list(self._subs.get(job_id, ())):
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                # 慢消费者：丢弃该事件，但不影响处理进度（前端可重新拉取全量排名）
                pass

    def schedule_ranking(self, job_id: str, producer: Callable[[], dict[str, Any]]) -> None:
        """请求一次排名推送，窗口内的多次请求合并为一次。

        `producer` 延迟到真正推送时才执行，因此拿到的一定是最新计数。
        """
        self._pending_rankings[job_id] = producer

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._ranking_delay)
            if not self._pending_rankings:
                continue
            pending, self._pending_rankings = self._pending_rankings, {}
            for job_id, producer in pending.items():
                try:
                    self.publish(job_id, "ranking.updated", producer())
                except Exception:
                    # 计数失败不应影响处理链路
                    pass
