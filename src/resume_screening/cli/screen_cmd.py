"""screen / review 命令（方案 §3 工作流）。

薄壳：装配适配器 → 调 core.pipeline → 打印摘要。不含业务逻辑。
"""

from __future__ import annotations

from pathlib import Path

import typer

from resume_screening.adapters.exporters.excel import ExcelExporter
from resume_screening.adapters.llm.factory import PROVIDER_OPTION_HELP, build_llm_client
from resume_screening.adapters.parsers.pdf_pymupdf import PdfPymupdfParser
from resume_screening.core.pipeline import run_screen
from resume_screening.core.review import apply_decisions, review_summary
from resume_screening.core.rubric import Rubric
from resume_screening.core.run_store import load_run, save_run


def screen(
    rubric: Path = typer.Option(..., "--rubric", exists=True, dir_okay=False, help="评分卡 JSON 路径"),
    input: Path = typer.Option(..., "--input", file_okay=False, help="简历目录"),
    out: Path = typer.Option(None, "--out", help="导出的 Excel 路径"),
    cache_dir: Path = typer.Option(Path("data/cache"), "--cache-dir", help="解析/提取缓存目录"),
    run_dir: Path = typer.Option(Path("data/runs"), "--run-dir", help="运行结果保存目录"),
    provider: str | None = typer.Option(None, "--provider", help=PROVIDER_OPTION_HELP),
    model: str = typer.Option(None, "--model", help="模型名，如 deepseek-chat / qwen-plus"),
    limit: int = typer.Option(None, "--limit", help="只处理前 N 份（调试用）"),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="忽略已有缓存、强制重算（新结果仍会写回缓存，供报告与界面复用）",
    ),
) -> None:
    """执行筛选并导出 Excel。"""
    try:
        r = Rubric.from_json(rubric)
    except Exception as e:  # noqa: BLE001
        typer.secho(f"评分卡加载失败：{e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    llm = build_llm_client(provider=provider, model=model)
    parser = PdfPymupdfParser()
    exporter = ExcelExporter()

    try:
        run, excel_path, usage = run_screen(
            input_dir=input,
            rubric=r,
            parser=parser,
            llm=llm,
            exporter=exporter,
            rubric_path=rubric,
            cache_dir=cache_dir,
            out_path=out,
            use_cache=not no_cache,
            limit=limit,
        )
    except FileNotFoundError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    run_json = save_run(run, run_dir / run.run_id / "run.json")
    typer.secho(f"筛选完成：{len(run.results)} 份简历", fg=typer.colors.GREEN)
    typer.echo(f"  run_id：{run.run_id}")
    typer.echo(f"  评分卡版本：{run.rubric_version}")
    typer.echo(f"  Excel：{excel_path}")
    typer.echo(f"  结果 JSON：{run_json}")
    typer.echo(f"  LLM 调用 {usage['calls']} 次 / token {usage['total_tokens']}")

    from collections import Counter

    stat = Counter(res.verdict.value for res in run.results)
    typer.echo(
        f"  通过 {stat.get('pass', 0)}｜待确认 {stat.get('needs_review', 0)}｜淘汰 {stat.get('reject', 0)}"
    )
    typer.echo("  下一步：HR 打开 Excel 填写『HR复核判定』列，再执行 review 回写。")


def review(
    excel: Path = typer.Option(..., "--excel", exists=True, dir_okay=False, help="HR 填写过的 Excel"),
    run: Path = typer.Option(..., "--run", exists=True, dir_okay=False, help="screen 生成的 run.json"),
    out: Path = typer.Option(None, "--out", help="回写后的结果 JSON 路径"),
) -> None:
    """把 Excel 中 HR 的人工判定回写到结果 JSON（v2 权重校准的唯一依据）。"""
    run_record = load_run(run)
    decisions = ExcelExporter().read_decisions(excel)
    if not decisions:
        typer.secho("未在 Excel 中读到任何 HR 判定（请填写『HR复核判定』列）", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    updated, unknown, invalid = apply_decisions(run_record, decisions)
    out_path = out or run.parent / "review.json"
    save_run(run_record, out_path)

    typer.secho(f"回写完成：{updated} 条", fg=typer.colors.GREEN)
    typer.echo(f"  输出：{out_path}")
    typer.echo(f"  分布：{review_summary(run_record)}")
    if unknown:
        typer.secho(f"  未匹配到结果的 resume_id：{unknown}", fg=typer.colors.YELLOW)
    if invalid:
        typer.secho(f"  非法判定值（应为 通过/淘汰/待确认）：{invalid}", fg=typer.colors.RED)


def register(app: typer.Typer) -> None:
    app.command()(screen)
    app.command()(review)
