"""导出 Excel（产品方案 §5.2：从主界面降级为导出功能）。

v0.1 的 Excel 是**主界面**，HR 在表里逐行勾选复核；v1.0 里主界面换成了网页，
Excel 退回它本来的位置——一份能发给别人、能留档的快照。

所以导出必须满足两件事：
1. **顺序与屏幕一致**：用 `sort_results` 排，而不是按数据库返回顺序，
   否则 HR 对着屏幕找第 5 个人，在表里是第 12 行。
2. **带上 HR 已做的复核**：`hr_decision` 回写进 Excel，导出的表不需要再对一遍。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..adapters.exporters.excel import ExcelExporter
from ..core.ids import new_run_id
from ..core.models import CandidateResult, RunRecord
from ..store import repo
from ..store.db import Database
from . import jobs as jobs_service
from .ranking import sort_results


@dataclass
class ExportResult:
    path: Path
    file_name: str
    rows: int
    version: str


def _safe_name(text: str) -> str:
    """去掉文件名里不能用的字符。岗位名里出现 `/`、`：` 是很常见的。"""
    cleaned = "".join(ch for ch in text if ch not in '\\/:*?"<>|').strip()
    return cleaned or "岗位"


def load_results(db: Database, job_id: str) -> tuple[list[CandidateResult], str]:
    """取当前标准下的全部结论（含 HR 复核判定），以及标准版本号。"""
    _, info = jobs_service.require_rubric(db, job_id)
    results: list[CandidateResult] = []
    for row in repo.list_results(db, job_id, info.rubric_id):
        try:
            result = CandidateResult.model_validate_json(row["payload"])
        except Exception:  # noqa: BLE001 - 单条坏数据不该让整份导出失败
            continue
        # 以库里的复核判定为准：payload 是评分当时的快照，复核之后没回写进去
        result.hr_decision = row["hr_decision"]
        results.append(result)
    return sort_results(results), info.version


def export_job(db: Database, job_id: str, out_dir: str | Path | None = None) -> ExportResult:
    """把当前标准下的排名导出成 Excel。"""
    job = jobs_service.require_job(db, job_id)
    results, version = load_results(db, job_id)

    directory = Path(out_dir) if out_dir else db.data_dir / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    file_name = f"{_safe_name(job.title)}-{_safe_name(version)}-初筛结果.xlsx"

    run = RunRecord(
        run_id=new_run_id(),
        rubric_version=version,
        rubric_path="",
        job_title=job.title,
        input_dir=str(db.data_dir),
        results=results,
    )
    path = ExcelExporter().export(run, directory / file_name)
    return ExportResult(path=path, file_name=file_name, rows=len(results), version=version)
