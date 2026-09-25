"""CLI 薄壳（方案 §4.1）：只做参数解析与展示，业务逻辑全在 core。

子命令：
    rubric new      —— 从 JD 生成评分卡草稿（M3）
    rubric validate —— 校验评分卡（合规 / 口径 / 版本）
    screen          —— 执行筛选并导出 Excel（M4/M5）
    review          —— HR 判定回写（M5）
"""

from __future__ import annotations

import typer

from resume_screening import __version__

app = typer.Typer(
    add_completion=False,
    help="AI 简历初筛 MVP：PDF 解析 → 结构化提取 → 评分卡 → 双层评估 → Excel 导出与复核",
)


@app.command()
def version() -> None:
    """显示版本号。"""
    typer.echo(f"resume-screening {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
