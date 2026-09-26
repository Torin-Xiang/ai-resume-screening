"""端到端编排（方案 §3 工作流）。

放在 core：接收注入的解析器 / 模型客户端 / 导出器（都只依赖 ports 协议），
CLI 与将来的 Web UI 可复用同一条流水线；核心不认识具体适配器。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .cache import ResumeCache
from .extractor import extract_resume
from .ids import new_run_id
from .llm import LLMClient, MeteredLLM
from .models import CandidateResult, ParseResult, ResumeDocument, RunRecord
from .ports import ResultExporter, ResumeParser
from .rubric import Rubric
from .scoring import evaluate_candidate, _sort_key

PDF_SUFFIXES = {".pdf"}

STAGE_PARSING = "parsing"
STAGE_EXTRACTING = "extracting"
STAGE_SCORING = "scoring"


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


def process_one(
    *,
    file_path: str | Path,
    rubric: Rubric,
    parser: ResumeParser,
    llm: LLMClient,
    cache: ResumeCache | None = None,
    use_cache: bool = True,
    on_stage: Callable[[str], None] | None = None,
) -> tuple[ResumeDocument, CandidateResult | None]:
    """处理**单份**简历：解析 → 提取（走缓存）→ 评估。

    与 `run_screen` 的区别是编排粒度：跑批要求"先凑齐一批"，而产品形态要求
    "来一份处理一份"（产品方案 §6.1）。判断逻辑完全一致，只是不再攒批。

    返回 `(doc, result)`：

    - 正常情况：`result` 为筛选结论
    - `result is None`：解析质量不达标（扫描件/无文本层），**未送入模型**，
      调用方应置为「需人工」并给出粘贴文本的出路——绝不允许静默跳过
      （产品方案 §6.1）。此处不返回一个"看起来像结论"的对象，就是为了
      让调用方无法忽略这条分支。

    `on_stage` 回调用于上报当前阶段（parsing/extracting/scoring），
    调用方据此更新处理状态并推送事件。core 不认识 HTTP，也不认识数据库。
    """

    def stage(name: str) -> None:
        if on_stage is not None:
            on_stage(name)

    stage(STAGE_PARSING)
    parsed = parser.parse(file_path)

    stage(STAGE_EXTRACTING)
    if cache is None:
        cache = ResumeCache()
    doc = extract_resume(parsed, llm, cache=cache, use_cache=use_cache)
    doc.source_path = str(file_path)  # 路径可能变（换机器/换目录），内容未变

    if not doc.parse_quality.usable:
        return doc, None

    stage(STAGE_SCORING)
    result = evaluate_candidate(doc, rubric, llm)
    return doc, result


def rescore_one(
    doc: ResumeDocument, rubric: Rubric, llm: LLMClient
) -> CandidateResult:
    """用新标准重算单份结论，**不重新解析、不重新提取**。

    这是"改标准后立即重排"（产品方案 §7 门槛标准）的实现依据：
    解析与提取的结果与标准无关，已缓存；换标准只是重跑评分这一层。
    """
    return evaluate_candidate(doc, rubric, llm)

