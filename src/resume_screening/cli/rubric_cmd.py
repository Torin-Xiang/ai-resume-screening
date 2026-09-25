"""评分卡子命令：new / validate / show（方案 §5.3）。

薄壳原则：这里只做参数解析、调用 core、打印结果；不写业务逻辑。
"""

from __future__ import annotations

from pathlib import Path

import typer

from resume_screening.adapters.llm.factory import build_llm_client
from resume_screening.core.rubric import Rubric, validate_rubric
from resume_screening.core.rubric_gen import draft_version, generate_rubric

rubric_app = typer.Typer(help="评分卡：从 JD 生成草稿 / 校验 / 查看")


@rubric_app.command("new")
def rubric_new(
    jd: str = typer.Option(None, "--jd", help="岗位 JD 文本"),
    jd_file: Path = typer.Option(
        None, "--jd-file", exists=True, dir_okay=False, help="JD 文本文件路径"
    ),
    out: Path = typer.Option(Path("rubrics/rubric.json"), "--out", help="评分卡输出路径"),
    version: str = typer.Option(None, "--version", help="业务版本号，默认 日期-draft"),
    job_title: str = typer.Option("", "--job-title", help="岗位名称"),
    provider: str = typer.Option("qwen", "--provider", help="模型 provider：qwen / fake"),
    model: str = typer.Option(None, "--model", help="模型名，如 qwen-plus"),
) -> None:
    """从 JD 生成评分卡草稿（生成后需人工编辑口径与权重）。"""
    if not jd and not jd_file:
        typer.secho("错误：需要提供 --jd 或 --jd-file", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    jd_text = jd or jd_file.read_text(encoding="utf-8")  # type: ignore[union-attr]

    llm = build_llm_client(provider=provider, model=model)
    rubric, dropped = generate_rubric(
        jd_text, llm, version=version or draft_version(), job_title=job_title
    )
    path = rubric.to_json(out)

    typer.secho(f"已生成评分卡草稿：{path}", fg=typer.colors.GREEN)
    typer.echo(f"  版本：{rubric.version}")
    typer.echo(f"  岗位：{rubric.job_title}")
    typer.echo(
        f"  必须项 {len(rubric.hard_filters)} / 加分项 {len(rubric.bonus)} / 减分项 {len(rubric.red_flags)}"
    )
    if dropped:
        typer.secho("  已剔除触碰合规红线的维度：", fg=typer.colors.YELLOW)
        for d in dropped:
            typer.echo(f"    - {d}")
    for w in validate_rubric(rubric):
        typer.secho(f"  提示：{w}", fg=typer.colors.YELLOW)
    typer.echo("  下一步：人工编辑该 JSON（口径 definition / 正反例 / 权重），再执行 screen。")


@rubric_app.command("validate")
def rubric_validate(path: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """校验评分卡：合规红线、口径完整性、版本。"""
    try:
        rubric = Rubric.from_json(path)
    except Exception as e:  # noqa: BLE001 - CLI 层统一转成友好报错
        typer.secho(f"评分卡不合法：{e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.secho(f"评分卡合法：{path}", fg=typer.colors.GREEN)
    typer.echo(f"  schema_version：{rubric.schema_version}")
    typer.echo(f"  业务版本：{rubric.version}")
    warnings = validate_rubric(rubric)
    if not warnings:
        typer.echo("  无警告")
    for w in warnings:
        typer.secho(f"  警告：{w}", fg=typer.colors.YELLOW)


@rubric_app.command("show")
def rubric_show(path: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    """打印评分卡维度摘要。"""
    rubric = Rubric.from_json(path)
    typer.echo(f"岗位：{rubric.job_title}｜版本：{rubric.version}｜满分：{rubric.max_possible_score()}")
    for kind, dims in (
        ("必须项 hard_filters", rubric.hard_filters),
        ("加分项 bonus", rubric.bonus),
        ("减分项 red_flags", rubric.red_flags),
    ):
        typer.echo(f"\n{kind}：")
        for d in dims:
            rule = f"rule={d.rule}" if getattr(d, "rule", None) else "LLM 判定"
            extra = ""
            if d.kind == "bonus":
                extra = f"｜权重 {d.weight}"
            elif d.kind == "red_flag":
                extra = f"｜扣分 {d.penalty}"
            typer.echo(f"  - [{d.id}] {d.name}（{rule}）{extra}")
            typer.echo(f"      口径：{d.definition}")
