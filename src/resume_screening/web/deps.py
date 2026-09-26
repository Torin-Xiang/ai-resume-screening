"""FastAPI 依赖注入：从 app.state 取装配好的对象。

路由函数只依赖这里暴露的接口，不自己去 new 数据库或模型客户端——
这样测试可以整体换掉装配（技术方案 §3.1 的分层要求）。
"""

from __future__ import annotations

from fastapi import Request

from ..core.llm import LLMClient
from ..core.ports import ResumeParser
from ..services.events import EventBus
from ..services.worker import ScreeningWorker
from ..store.db import Database


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_worker(request: Request) -> ScreeningWorker:
    return request.app.state.worker


def get_bus(request: Request) -> EventBus:
    return request.app.state.bus


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_parser(request: Request) -> ResumeParser:
    return request.app.state.parser
