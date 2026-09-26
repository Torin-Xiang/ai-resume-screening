"""SSE 事件流：处理状态与排名更新的实时推送（技术方案 §5.2）。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...services.events import EventBus
from ..deps import get_bus

router = APIRouter(prefix="/api/jobs", tags=["events"])

KEEP_ALIVE_SECONDS = 15.0


def encode_sse(event: str, data: dict) -> str:
    """SSE 帧格式。事件名 + 一行 JSON 数据。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/{job_id}/events")
async def stream_events(
    job_id: str, request: Request, bus: EventBus = Depends(get_bus)
) -> StreamingResponse:
    """订阅该岗位的处理事件。

    用 SSE 而不是轮询：上传后 HR 盯着的就是这张排名表，轮询既有延迟又浪费；
    SSE 让"处理中 → 完成 → 排到正确位置"这一串变化就地发生，不需要刷新页面。
    """

    async def generator():
        with bus.subscribe(job_id) as queue:
            yield encode_sse("connected", {"job_id": job_id})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=KEEP_ALIVE_SECONDS)
                except asyncio.TimeoutError:
                    # 心跳：防止中间代理把长时间无数据的连接掐掉
                    yield ": keep-alive\n\n"
                    continue
                yield encode_sse(event, data)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
