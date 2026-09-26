"""CLI 薄壳（方案 §4.1）：只做参数解析与展示，业务逻辑全在 core。

子命令：
    serve           —— 启动网页工作台（v1.0 的主入口）
    rubric new      —— 从 JD 生成评分卡草稿（M3）
    rubric validate —— 校验评分卡（合规 / 口径 / 版本）
    screen          —— 执行筛选并导出 Excel（M4/M5，v0.1 形态）
    review          —— HR 判定回写（M5，v0.1 形态）
"""

from __future__ import annotations

import typer

from resume_screening import __version__
from resume_screening.cli.rubric_cmd import rubric_app
from resume_screening.cli.screen_cmd import register as register_screen
from resume_screening.cli.serve_cmd import serve

app = typer.Typer(
    add_completion=False,
    help="AI 简历初筛：上传简历 + 岗位 JD，输出优先级排序与一句话理由",
)
app.add_typer(rubric_app, name="rubric")
register_screen(app)
app.command()(serve)


@app.command()
def version() -> None:
    """显示版本号。"""
    typer.echo(f"resume-screening {__version__}")


def main() -> None:  # pragma: no cover
    from dotenv import load_dotenv

    load_dotenv()  # 读取 .env 中的 DASHSCOPE_API_KEY
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
