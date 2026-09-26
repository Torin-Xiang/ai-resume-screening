"""用新标准重排（技术方案 §7 第②段的实现基础）。

核心卖点在这里：**改标准只重跑评分层，不重新解析、不重新提取。**

依据是数据模型的分层——解析与提取的结果（`resume_docs`）只取决于文件内容，
与标准无关，已经缓存；标准变了只需重算 `evaluate_candidate`。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..core.llm import LLMClient, MeteredLLM
from ..core.models import ResumeDocument
from ..core.pipeline import rescore_one
from ..core.rubric import Rubric
from ..store import repo
from ..store.db import Database
from ..store.entities import ResumeStatus
from .ranking import one_line_reason


@dataclass
class RescoreStats:
    """重排统计。`llm_calls` 可用于验证"没有重新解析"——
    完整处理是每份 2 次调用（提取 + 评分），重排应当只有每份 1 次（评分）。"""

    total: int = 0
    rescored: int = 0
    skipped_no_doc: int = 0
    skipped_unusable: int = 0
    failed: int = 0
    llm_calls: int = 0
    llm_tokens: int = 0


def _load_doc(db: Database, resume_id: str) -> ResumeDocument | None:
    return repo.get_resume_doc(db, resume_id)


async def rescore_job(
    *,
    db: Database,
    job_id: str,
    rubric: Rubric,
    rubric_id: str,
    llm: LLMClient,
    concurrency: int = 5,
) -> RescoreStats:
    """对该岗位所有简历用给定标准重算结论，写回 `results`。

    只处理解析质量达标的简历；扫描件等保持原状态（它们本来就没有可评的内容）。
    """
    records = repo.list_job_resumes(db, job_id)
    stats = RescoreStats(total=len(records))
    semaphore = asyncio.Semaphore(max(1, concurrency))
    lock = asyncio.Lock()

    async def one(record) -> None:
        doc = _load_doc(db, record.resume_id)
        if doc is None:
            # 还没解析过（比如刚上传还没轮到），跳过：它会走正常处理链路
            async with lock:
                stats.skipped_no_doc += 1
            return
        if not doc.parse_quality.usable:
            async with lock:
                stats.skipped_unusable += 1
            return

        # 每份用独立的计量包装，避免多线程共享计数器丢数
        metered = MeteredLLM(llm)
        async with semaphore:
            try:
                result = await asyncio.to_thread(rescore_one, doc, rubric, metered)
            except Exception:  # noqa: BLE001 - 单份失败不影响整体重排
                async with lock:
                    stats.failed += 1
                return

        # 与 worker 一致：结论里保留 HR 上传时的原始文件名，而不是存储用的 hash 名
        result.file_name = record.file_name

        repo.put_result(
            db,
            job_id=job_id,
            rubric_id=rubric_id,
            result=result,
            reason_line=one_line_reason(result),
        )
        if record.status == ResumeStatus.NEEDS_MANUAL.value:
            # 之前因解析质量搁置的，不在重排范围内；这里不改状态
            pass
        async with lock:
            stats.rescored += 1
            stats.llm_calls += metered.call_count
            stats.llm_tokens += metered.total_tokens

    await asyncio.gather(*(one(r) for r in records))
    return stats
