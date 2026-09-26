"""FastAPI 应用装配（技术方案 §3.1）。

分层硬约束：**web 只做三件事——校验入参、调用 services、返回结果。**
`core/` 与 `services/` 不认识 FastAPI，因此将来换界面（或加部署）不用重写业务逻辑。
"""

from __future__ import annotations

import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..adapters.llm.factory import build_llm_client
from ..adapters.parsers.pdf_pymupdf import PdfPymupdfParser
from ..services.events import EventBus
from ..services.jobs import JobNotFound, RubricNotSet
from ..services.worker import DEFAULT_CONCURRENCY, ScreeningWorker
from ..store.db import Database
from ..store.resume_cache import SqliteResumeCache
from .routers import events as events_routes
from .routers import jobs as jobs_routes
from .routers import ranking as ranking_routes
from .routers import resumes as resumes_routes

# 必须在挂载静态资源**之前**执行。
# Windows 注册表里 .js 的 Content-Type 可能是 text/plain，而 Python 的 mimetypes
# 会读注册表，于是前端脚本以 text/plain 下发，浏览器直接拒绝执行模块脚本
# （报 "Expected a JavaScript module script but the server responded with a MIME
# type of text/plain"），页面表现为一片空白。显式注册，避免依赖机器环境。
for _suffix, _mime in (
    (".js", "application/javascript"),
    (".mjs", "application/javascript"),
    (".css", "text/css"),
    (".json", "application/json"),
    (".svg", "image/svg+xml"),
    (".woff2", "font/woff2"),
    (".woff", "font/woff"),
):
    mimetypes.add_type(_mime, _suffix)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

UI_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
"""前端构建产物目录。存在则托管，不存在（尚未构建）则跳过。"""


def concurrency_from_env() -> int:
    """并发档位。默认 5，实测后由 `SCREEN_CONCURRENCY` 覆盖（技术方案 §10）。"""
    raw = os.getenv("SCREEN_CONCURRENCY", "")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_CONCURRENCY


def create_app(
    *,
    data_dir: str | Path | None = None,
    llm=None,
    parser=None,
    concurrency: int | None = None,
) -> FastAPI:
    """装配应用。所有外部依赖可注入，便于测试整体替换（不触网、不建真实库）。"""
    db = Database(data_dir).init_schema()
    bus = EventBus()
    client = llm if llm is not None else build_llm_client()
    resume_parser = parser if parser is not None else PdfPymupdfParser()
    worker = ScreeningWorker(
        db=db,
        parser=resume_parser,
        llm=client,
        bus=bus,
        cache=SqliteResumeCache(db),
        concurrency=concurrency if concurrency is not None else concurrency_from_env(),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        bus.start()
        await worker.start()
        # 上次进程留下的中间态任务重新排队，否则简历会永久卡在"处理中"
        worker.requeue_stuck()
        try:
            yield
        finally:
            await worker.stop()
            await bus.stop()

    app = FastAPI(title="AI 简历初筛工作台", version="1.0.0", lifespan=lifespan)

    app.state.db = db
    app.state.bus = bus
    app.state.worker = worker
    app.state.llm = client
    app.state.parser = resume_parser

    @app.exception_handler(JobNotFound)
    async def _job_not_found(_request, exc: JobNotFound):  # noqa: ANN202
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(RubricNotSet)
    async def _rubric_not_set(_request, exc: RubricNotSet):  # noqa: ANN202
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "provider": getattr(client, "name", "unknown"),
            "concurrency": worker.concurrency,
            "pending": worker.pending,
        }

    app.include_router(jobs_routes.router)
    app.include_router(resumes_routes.router)
    app.include_router(ranking_routes.router)
    app.include_router(events_routes.router)

    # 前端静态资源放在最后挂载，避免拦截 /api 路由
    if UI_DIST.is_dir():
        app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")

    return app


def run() -> None:  # pragma: no cover - 启动入口，逻辑简单无需单测
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()
    host = os.getenv("HOST", DEFAULT_HOST)
    port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
