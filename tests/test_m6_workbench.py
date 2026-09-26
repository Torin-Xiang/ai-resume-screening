"""M6：会话式工作台的后端主干。

覆盖（技术方案 §11 M6 完成标志）：

- 存储层与处理状态机，含 `needs_manual` / `failed` 两条异常路径
- 三分组：**「待确认」必须与「不推荐」分开**（产品方案 §6.3 的硬约定）
- 标准版本化：改标准不覆盖历史版本
- **改标准重排不重新解析**（本文件最核心的一条断言）
- HTTP 层：上传立即返回 202、格式不支持不静默丢弃、错误语义正确

全部使用 fake 模型与合成 PDF，不联网、不需要 API key。
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import write_pdf, write_scanned_pdf
from resume_screening.adapters.parsers.pdf_pymupdf import PdfPymupdfParser
from resume_screening.core.llm import LLMResponse, Usage
from resume_screening.core.models import CandidateResult, Judgement, ScreeningVerdict
from resume_screening.core.prompts import EXTRACT_SYSTEM
from resume_screening.core.rubric import Bonus, HardFilter, Rubric
from resume_screening.services import jobs as jobs_service
from resume_screening.services.ranking import (
    GROUP_NEEDS_REVIEW,
    GROUP_NOT_RECOMMEND,
    GROUP_RECOMMEND,
    build_ranking,
    group_of,
    one_line_reason,
)
from resume_screening.store import repo
from resume_screening.store.db import Database
from resume_screening.store.entities import ResumeStatus
from resume_screening.web.app import create_app

# ——————————————————————— 测试替身 ———————————————————————

EXTRACT_FIELDS: dict[str, dict] = {
    "name": {"value": "李示例", "status": "extracted", "evidence": {"quote": "李示例", "page": 1}},
    "contact": {
        "value": "13800000000",
        "status": "extracted",
        "evidence": {"quote": "13800000000", "page": 1},
    },
    "education": {"value": "本科", "status": "extracted", "evidence": {"quote": "本科", "page": 1}},
    "school": {
        "value": "示例大学",
        "status": "extracted",
        "evidence": {"quote": "示例大学", "page": 1},
    },
    "major": {
        "value": "计算机科学与技术",
        "status": "extracted",
        "evidence": {"quote": "计算机科学与技术", "page": 1},
    },
    "years_of_experience": {
        "value": 4,
        "status": "extracted",
        "evidence": {"quote": "2022.07-至今", "page": 1},
    },
    "current_company": {
        "value": "示例科技有限公司",
        "status": "extracted",
        "evidence": {"quote": "示例科技有限公司", "page": 1},
    },
    "current_title": {
        "value": "算法工程师",
        "status": "extracted",
        "evidence": {"quote": "算法工程师", "page": 1},
    },
    "skills": {
        "value": ["Python", "PyTorch"],
        "status": "extracted",
        "evidence": {"quote": "Python", "page": 1},
    },
}

SCORE_ITEM: dict = {
    "id": "ai_exp",
    "judgement": "satisfied",
    "hit_level": 1,
    "reason": "简历写明负责 RAG 系统上线，符合口径中的正例",
    "evidence": [{"quote": "负责 RAG 检索增强生成系统的设计与上线", "page": 1}],
}


class ScriptedLLM:
    """按 prompt 类型返回预设结果，并**分别统计两类调用次数**。

    提取次数是本文件的核心度量：改标准重排后它必须保持不变，
    否则就说明"不重跑解析"这条承诺没有被兑现。
    """

    name = "scripted"

    def __init__(self, *, latency: float = 0.0) -> None:
        self._latency = latency
        self._lock = threading.Lock()
        self.extract_calls = 0
        self.score_calls = 0

    @property
    def total_calls(self) -> int:
        return self.extract_calls + self.score_calls

    def complete_json(self, *, system: str, user: str, json_schema=None, temperature: float = 0.0):
        if self._latency:
            time.sleep(self._latency)
        usage = Usage(model="scripted", prompt_tokens=10, completion_tokens=5)
        with self._lock:
            if system == EXTRACT_SYSTEM:
                self.extract_calls += 1
                return LLMResponse(payload={"fields": EXTRACT_FIELDS}, usage=usage)
            self.score_calls += 1
            return LLMResponse(payload={"results": [dict(SCORE_ITEM)]}, usage=usage)


# ——————————————————————— 夹具与辅助 ———————————————————————


def make_rubric_dict(version: str = "v1", *, bonus_weight: float = 20.0) -> dict:
    """构造一份最小可用标准：一个规则硬门槛 + 一个软性加分项。

    加分项没有 `rule`，因此会走模型，用来验证"评分确实被执行了"。
    """
    return {
        "version": version,
        "job_title": "AI 应用开发工程师",
        "hard_filters": [
            {
                "id": "deg",
                "name": "本科及以上",
                "definition": "最高学历为本科及以上",
                "positive_examples": ["本科及以上"],
                "negative_examples": ["大专"],
                "rule": "min_degree",
                "params": {"min_level": "本科"},
            }
        ],
        "bonus": [
            {
                "id": "ai_exp",
                "name": "AI 项目经验",
                "definition": "实际参与过 AI 能力的设计或交付",
                "positive_examples": ["独立微调过模型", "搭建过 RAG 流程"],
                "negative_examples": ["仅调用接口写周报"],
                "weight": bonus_weight,
            }
        ],
        "red_flags": [],
    }


@contextmanager
def make_client(tmp_path: Path, *, llm: ScriptedLLM | None = None, concurrency: int = 1):
    """装配一个跑在临时数据目录上的应用，并返回 (client, llm)。"""
    scripted = llm or ScriptedLLM()
    app = create_app(
        data_dir=tmp_path / "appdata",
        llm=scripted,
        parser=PdfPymupdfParser(),
        concurrency=concurrency,
    )
    with TestClient(app) as client:
        yield client, scripted


def create_job(client: TestClient, *, title: str = "AI 应用开发工程师", rubric: dict | None = None) -> str:
    payload = {"title": title, "jd_text": "负责 RAG 与 Agent 应用落地"}
    if rubric is not None:
        payload["rubric"] = rubric
    resp = client.post("/api/jobs", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["job_id"]


def upload(client: TestClient, job_id: str, paths: list[Path]) -> dict:
    files = [
        ("files", (p.name, p.read_bytes(), "application/pdf"))
        for p in paths
    ]
    resp = client.post(f"/api/jobs/{job_id}/resumes", files=files)
    assert resp.status_code == 202, resp.text
    return resp.json()


def wait_settled(client: TestClient, job_id: str, *, timeout: float = 30.0) -> dict:
    """等待该岗位不再有待处理的简历，返回排名响应。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}/ranking").json()
        if last["pending"] == 0:
            return last
        time.sleep(0.05)
    raise AssertionError(f"处理未在 {timeout}s 内结束，最后状态：{last}")


# ——————————————————————— 存储层 ———————————————————————


def test_job_crud_and_counts(tmp_path: Path) -> None:
    db = Database(tmp_path / "d").init_schema()
    job = jobs_service.create_job(db, title="AI 应用开发工程师", jd_text="JD 文本")
    assert job.job_id.startswith("job_")

    fetched = jobs_service.require_job(db, job.job_id)
    assert fetched.title == "AI 应用开发工程师"
    assert fetched.resume_count == 0

    repo.upsert_job_resume(
        db, job_id=job.job_id, resume_id="r1", file_name="a.pdf", stored_path="/tmp/a.pdf"
    )
    repo.upsert_job_resume(
        db, job_id=job.job_id, resume_id="r2", file_name="b.pdf", stored_path="/tmp/b.pdf"
    )
    assert jobs_service.require_job(db, job.job_id).resume_count == 2
    # 两条都还是 queued → 待处理计数为 2
    assert jobs_service.require_job(db, job.job_id).pending_count == 2

    repo.set_resume_status(db, job.job_id, "r1", ResumeStatus.DONE)
    assert jobs_service.require_job(db, job.job_id).pending_count == 1


def test_default_job_hidden_from_list(tmp_path: Path) -> None:
    db = Database(tmp_path / "d").init_schema()
    job = jobs_service.create_job(db, title="临时岗位")
    assert len(jobs_service.list_jobs(db)) == 1
    jobs_service.delete_job(db, job.job_id)
    assert jobs_service.list_jobs(db) == []


def test_upsert_resume_is_idempotent(tmp_path: Path) -> None:
    """同一份文件重复上传不应产生两条记录（内容 hash 相同）。"""
    db = Database(tmp_path / "d").init_schema()
    job = jobs_service.create_job(db, title="岗位")
    first = repo.upsert_job_resume(
        db, job_id=job.job_id, resume_id="same", file_name="a.pdf", stored_path="/tmp/a.pdf"
    )
    second = repo.upsert_job_resume(
        db, job_id=job.job_id, resume_id="same", file_name="a.pdf", stored_path="/tmp/a.pdf"
    )
    assert first is True
    assert second is False
    assert len(repo.list_job_resumes(db, job.job_id)) == 1


def test_reset_stuck_resumes_requeues(tmp_path: Path) -> None:
    """进程重启后滞留在中间态的简历必须回到 queued，不能永远显示"处理中"。"""
    db = Database(tmp_path / "d").init_schema()
    job = jobs_service.create_job(db, title="岗位")
    repo.upsert_job_resume(
        db, job_id=job.job_id, resume_id="r1", file_name="a.pdf", stored_path="/tmp/a.pdf"
    )
    repo.set_resume_status(db, job.job_id, "r1", ResumeStatus.SCORING)
    repo.upsert_job_resume(
        db, job_id=job.job_id, resume_id="r2", file_name="b.pdf", stored_path="/tmp/b.pdf"
    )
    repo.set_resume_status(db, job.job_id, "r2", ResumeStatus.DONE)

    stuck = repo.list_pending_resumes(db)
    assert [r.resume_id for r in stuck] == ["r1"]
    assert repo.reset_stuck_resumes(db) == 1
    assert repo.get_job_resume(db, job.job_id, "r1").status == ResumeStatus.QUEUED.value
    # 已完成的不受影响
    assert repo.get_job_resume(db, job.job_id, "r2").status == ResumeStatus.DONE.value


# ——————————————————— 标准版本化 ———————————————————


def test_rubric_versions_are_not_overwritten(tmp_path: Path) -> None:
    db = Database(tmp_path / "d").init_schema()
    job = jobs_service.create_job(db, title="岗位")
    v1 = jobs_service.set_rubric(db, job.job_id, Rubric.model_validate(make_rubric_dict("v1")))
    v2 = jobs_service.set_rubric(db, job.job_id, Rubric.model_validate(make_rubric_dict("v2")))

    assert v1.rubric_id != v2.rubric_id
    versions = [v.version for v in jobs_service.list_rubric_versions(db, job.job_id)]
    assert versions == ["v2", "v1"]  # 最新的在前
    assert jobs_service.current_rubric_info(db, job.job_id).version == "v2"


def test_duplicate_version_gets_suffix(tmp_path: Path) -> None:
    """版本号冲突时自动追加序号——绝不覆盖历史版本，否则无法回溯。"""
    db = Database(tmp_path / "d").init_schema()
    job = jobs_service.create_job(db, title="岗位")
    jobs_service.set_rubric(db, job.job_id, Rubric.model_validate(make_rubric_dict("same")))
    second = jobs_service.set_rubric(db, job.job_id, Rubric.model_validate(make_rubric_dict("same")))
    assert second.version == "same-2"
    assert len(jobs_service.list_rubric_versions(db, job.job_id)) == 2


def test_missing_version_is_auto_numbered(tmp_path: Path) -> None:
    db = Database(tmp_path / "d").init_schema()
    job = jobs_service.create_job(db, title="岗位")
    payload = make_rubric_dict("")
    first = jobs_service.set_rubric(db, job.job_id, Rubric.model_validate(payload))
    second = jobs_service.set_rubric(db, job.job_id, Rubric.model_validate(payload))
    assert (first.version, second.version) == ("v1", "v2")


# ——————————————————————— 三分组 ———————————————————————


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (ScreeningVerdict.PASS, GROUP_RECOMMEND),
        (ScreeningVerdict.NEEDS_REVIEW, GROUP_NEEDS_REVIEW),
        (ScreeningVerdict.REJECT, GROUP_NOT_RECOMMEND),
    ],
)
def test_group_mapping(verdict: ScreeningVerdict, expected: str) -> None:
    assert group_of(verdict) == expected
    assert group_of(verdict.value) == expected


def _result(verdict: ScreeningVerdict, dims: list) -> CandidateResult:
    return CandidateResult(
        resume_id="r1",
        file_name="a.pdf",
        source_path="/tmp/a.pdf",
        verdict=verdict,
        score=10,
        dimensions=dims,
    )


def test_ranking_keeps_needs_review_separate_from_reject() -> None:
    """三分组的意义：简历没写到的人（待确认）不能被混进不推荐里一起丢掉。"""
    from resume_screening.core.models import DimensionResult

    docs = [
        _result(
            ScreeningVerdict.PASS,
            [DimensionResult(dimension_id="a", name="学历", kind="hard_filter", judgement=Judgement.SATISFIED)],
        ),
        _result(
            ScreeningVerdict.NEEDS_REVIEW,
            [
                DimensionResult(
                    dimension_id="a", name="学历", kind="hard_filter", judgement=Judgement.NOT_MENTIONED
                )
            ],
        ),
        _result(
            ScreeningVerdict.REJECT,
            [
                DimensionResult(
                    dimension_id="a", name="学历", kind="hard_filter", judgement=Judgement.NOT_SATISFIED
                )
            ],
        ),
    ]
    ranking = build_ranking(docs)
    assert [g["key"] for g in ranking["groups"]] == [
        GROUP_RECOMMEND,
        GROUP_NEEDS_REVIEW,
        GROUP_NOT_RECOMMEND,
    ]
    assert ranking["counts"] == {GROUP_RECOMMEND: 1, GROUP_NEEDS_REVIEW: 1, GROUP_NOT_RECOMMEND: 1}


def test_reason_points_to_specific_dimension() -> None:
    """一句话理由必须指向具体标准，不能是"综合素质较好"这类空话。"""
    from resume_screening.core.models import DimensionResult, Evidence

    missing = _result(
        ScreeningVerdict.NEEDS_REVIEW,
        [
            DimensionResult(
                dimension_id="ai",
                name="AI 项目经验",
                kind="hard_filter",
                judgement=Judgement.NOT_MENTIONED,
            )
        ],
    )
    assert "AI 项目经验" in one_line_reason(missing)

    blocked = _result(
        ScreeningVerdict.REJECT,
        [
            DimensionResult(
                dimension_id="deg",
                name="本科及以上",
                kind="hard_filter",
                judgement=Judgement.NOT_SATISFIED,
                evidence=[Evidence(quote="学历：大专")],
            )
        ],
    )
    reason = one_line_reason(blocked)
    assert "本科及以上" in reason
    assert len(reason) <= 30


# ——————————————————— 端到端（HTTP） ———————————————————


def test_upload_ranks_end_to_end(tmp_path: Path) -> None:
    with make_client(tmp_path) as (client, llm):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        pdf = write_pdf(tmp_path / "a.pdf", [*_resume_items()])

        body = upload(client, job_id, [pdf])
        assert body["accepted"] == 1
        assert body["items"][0]["status"] == "queued"

        ranking = wait_settled(client, job_id)
        assert ranking["total"] == 1
        assert ranking["rubric_version"] == "v1"
        # 学历规则满足、AI 加分项命中 → 推荐
        assert ranking["counts"][GROUP_RECOMMEND] == 1
        item = ranking["groups"][0]["items"][0]
        assert item["candidate_name"] == "李示例"
        # 给 HR 看的必须是上传时的原名，而不是存储用的内容 hash 名
        assert item["file_name"] == "a.pdf"
        assert item["reason"]
        # 展开可见原文证据（每条结论都能被核对）
        assert any(d["evidence"] for d in item["dimensions"])

        assert llm.extract_calls == 1
        assert llm.score_calls == 1


def test_upload_returns_before_processing_finishes(tmp_path: Path) -> None:
    """上传必须立刻返回：同步处理会让十份简历直接超时，用户看到转圈然后失败。

    断言方式刻意是**相对**的：拿"上传耗时"和注入的模型延迟比，而不是和某个
    绝对秒数比。写死 0.3 秒曾经在机器变忙时误报——测试想验的是"上传没有等待处理"，
    不是"这台机器有多快"。模型延迟给足 2 秒，门槛就有足够余量：
    只要处理被放进请求里，耗时必然 ≥2 秒，一定被抓到。
    """
    latency = 2.0
    slow = ScriptedLLM(latency=latency)
    with make_client(tmp_path, llm=slow) as (client, _):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        pdf = write_pdf(tmp_path / "a.pdf", [*_resume_items()])

        started = time.time()
        body = upload(client, job_id, [pdf])
        elapsed = time.time() - started

        assert body["accepted"] == 1
        assert elapsed < latency / 2, (
            f"上传接口耗时 {elapsed:.2f}s，已接近模型延迟 {latency}s，说明处理被放在请求内了"
        )
        # 此刻确实还没处理完
        assert client.get(f"/api/jobs/{job_id}/ranking").json()["pending"] == 1
        wait_settled(client, job_id)


def test_scanned_pdf_marked_needs_manual(tmp_path: Path) -> None:
    """扫描件不送模型，且**绝不静默跳过**——HR 必须看到"这份要人工处理"。"""
    with make_client(tmp_path) as (client, llm):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        scanned = write_scanned_pdf(tmp_path / "scanned.pdf")

        upload(client, job_id, [scanned])
        wait_settled(client, job_id)

        resumes = client.get(f"/api/jobs/{job_id}/resumes").json()["items"]
        assert resumes[0]["status"] == ResumeStatus.NEEDS_MANUAL.value
        assert resumes[0]["error"], "必须说明为什么需要人工处理"


def test_watermarked_image_pdf_is_held_back_and_reason_is_actionable(
    tmp_path: Path,
) -> None:
    """图片型 PDF（只有水印文本层）→ 不送模型，且理由必须说清是"图片/水印"导致。

    这条锁的是**整条链路**：解析器判定 → worker 取 `parse_quality.reasons` → 入库 →
    接口下发 → 界面渲染。真实样本上出过的问题就在链路最前端：
    水印把字符数抬过阈值，判定退化成 suspect，于是这份简历**被送进了模型**，
    模型只能对着水印编字段——看起来"处理成功"，实则全是编造，比报错更危险。

    断言理由里必须出现"图片"或"水印"，而不只是"非空"：
    只判非空的话，退化成笼统的"解析质量存疑"也能过，HR 仍然不知道该做什么。
    """
    from tests.conftest import write_watermarked_image_pdf

    with make_client(tmp_path) as (client, llm):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        sample = write_watermarked_image_pdf(tmp_path / "wm_image.pdf")

        upload(client, job_id, [sample])
        wait_settled(client, job_id)

        item = client.get(f"/api/jobs/{job_id}/resumes").json()["items"][0]
        assert item["status"] == ResumeStatus.NEEDS_MANUAL.value
        assert item["error"], "必须说明为什么需要人工处理"
        assert ("图片" in item["error"]) or ("水印" in item["error"]), (
            f"理由必须指向真正的成因（图片/水印），实际：{item['error']}"
        )
        # 绝不能出现在排名里——那意味着它被当成"看过并放过"
        ranking = client.get(f"/api/jobs/{job_id}/ranking").json()
        assert all(
            r["resume_id"] != item["resume_id"]
            for g in ranking["groups"]
            for r in g["items"]
        ), "需人工处理的简历不得进入排名"

        ranking = client.get(f"/api/jobs/{job_id}/ranking").json()
        assert ranking["total"] == 0  # 未评估，不出现在排名里
        assert llm.score_calls == 0  # 解析不达标 → 不调评分


def test_unsupported_format_is_reported_not_swallowed(tmp_path: Path) -> None:
    """格式不支持要明说，不能悄悄丢掉——否则 HR 以为传了、其实没进来。"""
    with make_client(tmp_path) as (client, _):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        resp = client.post(
            f"/api/jobs/{job_id}/resumes",
            files=[("files", ("a.docx", b"not a pdf", "application/octet-stream"))],
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["accepted"] == 0
        assert len(body["rejected"]) == 1
        assert "docx" in body["rejected"][0]["reason"]


def test_failed_parse_marks_failed(tmp_path: Path) -> None:
    """损坏文件 → failed 状态并带原因，不能卡在"处理中"。"""
    with make_client(tmp_path) as (client, _):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"%PDF-1.4 this is not a real pdf")

        resp = client.post(
            f"/api/jobs/{job_id}/resumes",
            files=[("files", ("broken.pdf", broken.read_bytes(), "application/pdf"))],
        )
        assert resp.status_code == 202
        wait_settled(client, job_id)

        resumes = client.get(f"/api/jobs/{job_id}/resumes").json()["items"]
        assert resumes[0]["status"] == ResumeStatus.FAILED.value
        assert resumes[0]["error"]


# ——————— 核心：改标准只重算评分，不重新解析 ———————


def test_rescore_reuses_parse_cache(tmp_path: Path) -> None:
    """本文件最重要的一条：换标准后**提取调用数不变**，只有评分重跑。

    这是产品方案 §7「改标准后立即重排」与 §2（省成本）的实现保证。
    如果哪天有人把重排写成"重新走一遍解析提取"，这个测试会立刻失败。
    """
    with make_client(tmp_path) as (client, llm):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        pdfs = [
            write_pdf(tmp_path / "a.pdf", [*_resume_items()]),
            write_pdf(tmp_path / "b.pdf", [*_resume_items(offset=3)]),
        ]
        upload(client, job_id, pdfs)
        wait_settled(client, job_id)

        assert llm.extract_calls == 2
        assert llm.score_calls == 2

        # 业务口径变了：改权重，走 PUT rubric（内部触发重排）
        new_rubric = make_rubric_dict("v2", bonus_weight=35.0)
        resp = client.put(
            f"/api/jobs/{job_id}/rubric", json={"rubric": new_rubric, "note": "加分权重上调"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["version"] == "v2"

        # 关键断言：提取一次都没重跑
        assert llm.extract_calls == 2, "改标准不应重新解析简历"
        assert llm.score_calls == 4, "评分层应当整体重算"

        # 历史版本仍在，可回溯、可对比
        versions = client.get(f"/api/jobs/{job_id}/rubric/versions").json()
        assert [v["version"] for v in versions] == ["v2", "v1"]

        ranking = wait_settled(client, job_id)
        assert ranking["rubric_version"] == "v2"
        assert ranking["total"] == 2


def test_rebuild_endpoint_rescores_current_rubric(tmp_path: Path) -> None:
    with make_client(tmp_path) as (client, llm):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        pdf = write_pdf(tmp_path / "a.pdf", [*_resume_items()])
        upload(client, job_id, [pdf])
        wait_settled(client, job_id)
        before = llm.score_calls

        resp = client.post(f"/api/jobs/{job_id}/ranking/rebuild")
        assert resp.status_code == 200, resp.text
        stats = resp.json()
        assert stats["rescored"] == 1
        assert llm.extract_calls == 1
        assert llm.score_calls == before + 1


# ——————————————————— 错误语义 ———————————————————


def test_unknown_job_returns_404(tmp_path: Path) -> None:
    with make_client(tmp_path) as (client, _):
        assert client.get("/api/jobs/job_nope").status_code == 404
        assert client.get("/api/jobs/job_nope/ranking").status_code == 404
        assert client.post("/api/jobs/job_nope/resumes", files=[]).status_code in (404, 422)


def test_invalid_rubric_returns_422(tmp_path: Path) -> None:
    """合规敏感项混进标准时必须被拒（产品方案 §4 合规红线）。"""
    with make_client(tmp_path) as (client, _):
        bad = make_rubric_dict("v1")
        bad["hard_filters"].append(
            {
                "id": "age",
                "name": "年龄不超过 35",
                "definition": "候选人年龄不超过 35 岁",
                "positive_examples": ["30 岁"],
                "negative_examples": ["40 岁"],
                "rule": None,
                "params": {},
            }
        )
        resp = client.post("/api/jobs", json={"title": "岗位", "jd_text": "", "rubric": bad})
        assert resp.status_code == 422


def test_upload_to_job_without_rubric_marks_failed(tmp_path: Path) -> None:
    """没设标准就上传：必须明确失败并给出原因，而不是默默什么都不做。"""
    with make_client(tmp_path) as (client, _):
        job_id = create_job(client)  # 不带 rubric
        pdf = write_pdf(tmp_path / "a.pdf", [*_resume_items()])
        upload(client, job_id, [pdf])
        wait_settled(client, job_id)

        resumes = client.get(f"/api/jobs/{job_id}/resumes").json()["items"]
        assert resumes[0]["status"] == ResumeStatus.FAILED.value
        assert "标准" in resumes[0]["error"]


def test_health_endpoint(tmp_path: Path) -> None:
    with make_client(tmp_path) as (client, _):
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["provider"] == "scripted"


def test_js_mime_type_is_javascript() -> None:
    """Windows 注册表会把 .js 映射成 text/plain，浏览器据此拒绝执行模块脚本，
    页面表现为一片空白。应用必须显式注册 MIME，不能依赖机器环境。"""
    import mimetypes

    import resume_screening.web.app  # noqa: F401  导入即完成注册

    assert mimetypes.guess_type("index.js")[0] == "application/javascript"
    assert mimetypes.guess_type("index.css")[0] == "text/css"


def test_served_frontend_asset_has_correct_mime(tmp_path: Path) -> None:
    """前端构建产物由后端托管时，Content-Type 必须正确——否则浏览器白屏。"""
    assets = Path(__file__).resolve().parents[1] / "web" / "dist" / "assets"
    scripts = sorted(assets.glob("*.js")) if assets.is_dir() else []
    if not scripts:
        pytest.skip("前端尚未构建（web/dist/assets 为空），跳过静态托管检查")

    with make_client(tmp_path) as (client, _):
        resp = client.get(f"/assets/{scripts[0].name}")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]


# ——————————————————— 事件总线 ———————————————————


def test_event_bus_merges_ranking_updates(tmp_path: Path) -> None:
    """排名推送要合并：批量上传时每份都推会刷屏。"""
    from resume_screening.services.events import EventBus

    async def scenario() -> list[tuple[str, dict]]:
        bus = EventBus(ranking_delay=0.05)
        bus.start()
        received: list[tuple[str, dict]] = []
        with bus.subscribe("job_1") as queue:
            for i in range(5):
                bus.schedule_ranking("job_1", lambda i=i: {"n": i})
            await asyncio.sleep(0.2)
            while not queue.empty():
                received.append(queue.get_nowait())
        await bus.stop()
        return received

    events = asyncio.run(scenario())
    ranking_events = [e for e in events if e[0] == "ranking.updated"]
    assert len(ranking_events) == 1, f"应合并为 1 次，实际 {len(ranking_events)} 次"
    # 合并后取到的是最后一次（最新计数），而不是最早那次
    assert ranking_events[0][1]["n"] == 4


def test_event_bus_does_not_block_slow_subscriber(tmp_path: Path) -> None:
    """慢消费者只会丢自己的事件，不能拖住后台处理。"""
    from resume_screening.services.events import MAX_QUEUE_SIZE, EventBus

    async def scenario() -> int:
        bus = EventBus()
        with bus.subscribe("job_1") as queue:
            for i in range(MAX_QUEUE_SIZE + 20):
                bus.publish("job_1", "resume.status", {"i": i})
            return queue.qsize()

    assert asyncio.run(scenario()) == MAX_QUEUE_SIZE


# ——————————————————————— 辅助数据 ———————————————————————


def _resume_items(*, offset: float = 0) -> list[tuple[float, float, str]]:
    """合成简历（虚构人物），覆盖 9 个字段提取所需的原文片段。"""
    base = [
        (50, 60, "个人简历"),
        (50, 90, "姓名：李示例"),
        (50, 110, "联系电话：13800000000 邮箱：example@mail.com"),
        (50, 140, "教育背景：2018.09-2022.06 示例大学 计算机科学与技术 本科"),
        (50, 170, "工作经历：2022.07-至今 示例科技有限公司 算法工程师"),
        (50, 200, "负责 RAG 检索增强生成系统的设计与上线"),
        (50, 225, "独立完成开源大模型微调并部署到生产环境"),
        (50, 250, "技能：Python、PyTorch、RAG、Agent、Docker"),
    ]
    if not offset:
        return base
    return [(x, y + offset, text) for x, y, text in base]
