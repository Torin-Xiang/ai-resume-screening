"""端到端编排（方案 §3 工作流）。

放在 core：接收注入的解析器 / 模型客户端 / 导出器（都只依赖 ports 协议），
CLI 与将来的 Web UI 可复用同一条流水线；核心不认识具体适配器。
"""

from __future__ import annotations

from pathlib import Path

from .cache import ResumeCache
from .extractor import extract_resume
from .ids import new_run_id
from .llm import LLMClient, MeteredLLM
from .models import ParseResult, ResumeDocument, RunRecord
from .ports import ResultExporter, ResumeParser
from .rubric import Rubric
from .scoring import evaluate_candidate, _sort_key

PDF_SUFFIXES = {".pdf"}


def collect_resumes(input_dir: str | Path, parser: ResumeParser) -> list[Path]:
    """收集该解析器支持的简历文件（按文件名排序，保证结果可复现）。"""
    d = Path(input_dir)
    if not d.is_dir():
        raise NotADirectoryError(f"简历目录不存在：{d}")
    return sorted(
        p for p in d.iterdir() if p.is_file() and p.suffix.lower() in {s.lower() for s in parser.suffixes}
    )


def run_screen(
    *,
    input_dir: str | Path,
    rubric: Rubric,
    parser: ResumeParser,
    llm: LLMClient,
    exporter: ResultExporter,
    rubric_path: str | Path = "",
    cache_dir: str | Path = "data/cache",
    out_path: str | Path | None = None,
    run_id: str | None = None,
    use_cache: bool = True,
    limit: int | None = None,
) -> tuple[RunRecord, Path, dict[str, int]]:
    """完整跑一遍筛选：解析 → 提取（缓存）→ 评分 → 导出。返回 (run, excel 路径, 计量)。"""
    files = collect_resumes(input_dir, parser)
    if limit:
        files = files[:limit]
    if not files:
        raise FileNotFoundError(f"目录中没有可用于解析的简历文件：{input_dir}")

    rid = run_id or new_run_id()
    cache = ResumeCache(cache_dir)
    metered = MeteredLLM(llm)

    # ① 解析 + ② 提取（命中缓存则不重复调用模型）
    docs: list[ResumeDocument] = []
    for f in files:
        parsed: ParseResult = parser.parse(f)
        docs.append(extract_resume(parsed, metered, cache=cache, use_cache=use_cache))

    # ③ 评分
    results = [evaluate_candidate(doc, rubric, metered) for doc in docs]
    results.sort(key=_sort_key)

    run = RunRecord(
        run_id=rid,
        rubric_version=rubric.version,
        rubric_path=str(rubric_path),
        job_title=rubric.job_title,
        input_dir=str(input_dir),
        results=results,
        llm_call_count=metered.call_count,
        llm_cost_tokens=metered.total_tokens,
    )

    # ④ 导出
    out = Path(out_path) if out_path else Path("data/output") / rid / "初筛结果.xlsx"
    path = exporter.export(run, out)
    return run, path, metered.snapshot()
