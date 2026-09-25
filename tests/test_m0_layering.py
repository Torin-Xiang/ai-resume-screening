"""架构分层测试（方案 §4.1）。

判断耦合度的标准：**新增解析器不应导致核心测试改动**。
因此核心库必须不认识 CLI、不认识具体适配器（SDK / PyMuPDF / openpyxl）。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "resume_screening"
CORE_DIR = SRC / "core"

FORBIDDEN_PREFIXES = (
    "resume_screening.cli",
    "resume_screening.adapters",
)
FORBIDDEN_MODULES = ("fitz", "pymupdf", "openpyxl", "openai", "typer", "pandas")


def _core_files() -> list[Path]:
    return sorted(CORE_DIR.glob("*.py"))


def test_core_directory_exists_and_non_empty() -> None:
    assert CORE_DIR.is_dir()
    assert _core_files(), "core 目录为空"


def test_core_must_not_import_cli_or_adapters() -> None:
    offenders: list[str] = []
    for f in _core_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for n in names:
                if n and any(n.startswith(p) for p in FORBIDDEN_PREFIXES):
                    offenders.append(f"{f.name}: import {n}")
    assert not offenders, f"核心库不得依赖 CLI / 适配器层：{offenders}"


def test_core_must_not_import_concrete_sdks() -> None:
    offenders: list[str] = []
    for f in _core_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for n in names:
                if n in FORBIDDEN_MODULES:
                    offenders.append(f"{f.name}: import {n}")
    assert not offenders, f"核心库不得直接依赖具体 SDK/库（应由适配器承担）：{offenders}"


def test_adapter_layer_has_three_kinds() -> None:
    """解析器 / 模型客户端 / 导出器三类适配器目录齐备（M0 先建占位包）。"""
    adapters = SRC / "adapters"
    for sub in ("parsers", "llm", "exporters"):
        assert (adapters / sub).is_dir(), f"缺少适配器目录：{sub}"
