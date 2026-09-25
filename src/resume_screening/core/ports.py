"""适配器端口（Protocol）：核心库只依赖这些抽象，不依赖具体实现。

新增能力 = 实现一个新的适配器并注册，核心逻辑不动（方案 §4.1）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import CandidateResult, ParseResult, RunRecord


@runtime_checkable
class ResumeParser(Protocol):
    """解析器：纯函数（文件路径 → ParseResult），不掺评分逻辑与全局状态（方案 §9）。"""

    name: str
    suffixes: tuple[str, ...]

    def parse(self, path: str | Path) -> ParseResult: ...


@runtime_checkable
class ResultExporter(Protocol):
    """导出器：把一次运行结果写成 HR 可直接使用的文件。"""

    name: str

    def export(self, run: RunRecord, out_path: str | Path) -> Path: ...


@runtime_checkable
class ReviewReader(Protocol):
    """复核回写：从导出的文件中读回 HR 人工判定。"""

    name: str

    def read_decisions(self, path: str | Path) -> dict[str, Any]: ...
