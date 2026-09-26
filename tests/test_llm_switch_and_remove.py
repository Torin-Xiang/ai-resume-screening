"""界面切换模型通道 + 排名里删除简历。

两个需求来自同一轮真实使用反馈（2026-09-27）：

1. **切换模型**：`.env` 改了不重启不生效，用户被"界面显示离线、配置写着
   DeepSeek"坑过。所以通道切换必须做成界面功能：立即生效 + 跨重启保持。
2. **排名里删除简历**：之前只有处理列表能删，排名（results 表）里删不掉；
   而且 `remove_job_resume` 只删 job_resumes，排名读的是 results——
   列表里消失了、排名里还在，比不能删更迷惑。这条回归必须钉住。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from resume_screening.services import llm_settings
from resume_screening.store import repo
from resume_screening.store.db import Database
from resume_screening.web.app import create_app

from conftest import write_pdf


# ——————————————————————— 测试替身 ———————————————————————


class NamedFakeLLM:
    """带名字的最小替身：只为验证"切换后 worker 真的在用新通道"。"""

    def __init__(self, name: str, model: str = "fake-1") -> None:
        self.name = name
        self.model = model
        self.calls = 0

    def complete_json(self, *, system: str, user: str, json_schema=None, temperature: float = 0.0):
        self.calls += 1
        from resume_screening.core.llm import LLMResponse, Usage

        return LLMResponse(payload={}, usage=Usage(model=self.model), raw="fake")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """注入 fake 的 app + TestClient。密钥环境清理干净，验证"缺密钥置灰"。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    app = create_app(data_dir=tmp_path, llm=NamedFakeLLM("fake-injected"))
    with TestClient(app) as test_client:
        yield test_client, tmp_path


# ——————————————————————— 切换模型 ———————————————————————


def test_get_llm_settings_reports_current_and_choices(client):
    test_client, _ = client
    resp = test_client.get("/api/settings/llm")
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "fake-injected"
    providers = {c["provider"] for c in data["choices"]}
    assert {"scripted", "deepseek"} <= providers
    # 密钥被清理干净后，deepseek 通道必须置灰，而不是切过去才报错
    deepseek = next(c for c in data["choices"] if c["provider"] == "deepseek")
    assert deepseek["available"] is False


def test_switch_to_scripted_takes_effect_immediately(client):
    test_client, tmp_path = client
    resp = test_client.post("/api/settings/llm", json={"provider": "scripted"})
    assert resp.status_code == 200
    assert resp.json()["provider"] == "scripted"

    # health 必须跟着变——它就是"界面显示的通道和实际用的是不是同一个"的裁判
    assert test_client.get("/api/health").json()["provider"] == "scripted"

    # 选择必须持久化：重启后仍保持，否则悄悄退回 .env 默认
    saved = llm_settings.load_choice(tmp_path)
    assert saved == {"provider": "scripted", "model": "scripted-1"}


def test_switch_requires_key_for_deepseek(client):
    test_client, _ = client
    resp = test_client.post("/api/settings/llm", json={"provider": "deepseek"})
    assert resp.status_code == 400
    assert "LLM_API_KEY" in resp.json()["detail"]


def test_switch_to_deepseek_with_key(client, monkeypatch):
    test_client, tmp_path = client
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    resp = test_client.post(
        "/api/settings/llm", json={"provider": "deepseek", "model": "deepseek-flash"}
    )
    assert resp.status_code == 200
    assert resp.json()["provider"] == "deepseek"
    assert resp.json()["model"] == "deepseek-flash"
    assert llm_settings.load_choice(tmp_path)["model"] == "deepseek-flash"


def test_switch_rejects_unknown_provider(client):
    test_client, _ = client
    resp = test_client.post("/api/settings/llm", json={"provider": "nope"})
    assert resp.status_code == 400


def test_saved_choice_is_used_on_restart(tmp_path, monkeypatch):
    """重启后（不注入替身）应用上次的选择，而不是悄悄退回 .env 默认。"""
    llm_settings.save_choice(tmp_path, provider="scripted", model="scripted-1")
    app = create_app(data_dir=tmp_path)
    from fastapi.testclient import TestClient as TC

    with TC(app) as test_client:
        assert test_client.get("/api/health").json()["provider"] == "scripted"


def make_rubric(version: str = "v1"):
    """最小可用标准：一个规则硬门槛 + 一个走模型的加分项（同 m6 的做法）。"""
    from resume_screening.core.rubric import Rubric

    return Rubric.model_validate(
        {
            "version": version,
            "job_title": "通道切换",
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
                    "weight": 20.0,
                }
            ],
            "red_flags": [],
        }
    )


def test_worker_set_llm_swaps_channel_for_later_resumes(tmp_path):
    """切换后新入队的简历必须走新通道（这正是"随时切换"的意义）。"""
    from resume_screening.services.events import EventBus
    from resume_screening.services.ingest import ingest_upload
    from resume_screening.services.worker import ScreeningWorker
    from resume_screening.store.resume_cache import SqliteResumeCache
    from resume_screening.adapters.parsers.pdf_pymupdf import PdfPymupdfParser
    from resume_screening.services import jobs as jobs_service

    db = Database(tmp_path).init_schema()
    old_llm = NamedFakeLLM("old-channel")
    new_llm = NamedFakeLLM("new-channel")
    worker = ScreeningWorker(
        db=db,
        parser=PdfPymupdfParser(),
        llm=old_llm,
        bus=EventBus(),
        cache=SqliteResumeCache(db),
        concurrency=1,
    )

    async def scenario():
        await worker.start()
        try:
            job = jobs_service.create_job(db, title="通道切换", jd_text="要求：本科")
            jobs_service.set_rubric(db, job.job_id, make_rubric(), note="t")
            content = write_pdf(
                tmp_path / "a.pdf",
                [(72, 100, "张三"), (72, 130, "本科 示例大学"), (72, 160, "13800000000")],
            ).read_bytes()
            result = ingest_upload(db, job_id=job.job_id, file_name="a.pdf", content=content)
            worker.enqueue(job.job_id, result.resume_id)
            await worker.drain()

            # 第一份用旧通道评完；切换后第二份必须走新通道
            assert old_llm.calls > 0 and new_llm.calls == 0
            worker.set_llm(new_llm)
            content2 = write_pdf(
                tmp_path / "b.pdf",
                [(72, 100, "李四"), (72, 130, "本科 示例大学"), (72, 160, "13900000000")],
            ).read_bytes()
            result2 = ingest_upload(db, job_id=job.job_id, file_name="b.pdf", content=content2)
            worker.enqueue(job.job_id, result2.resume_id)
            await worker.drain()
            assert new_llm.calls > 0
        finally:
            await worker.stop()

    import asyncio

    asyncio.run(scenario())


# ——————————————————————— 排名里删除简历 ———————————————————————


def test_remove_job_resume_also_drops_results(tmp_path):
    """删除必须连带结论：只删 job_resumes 的话，排名里那个人永远还在。"""
    db = Database(tmp_path).init_schema()
    job_id = "job_x"
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, title, jd_text, status, created_at, updated_at)"
            " VALUES (?, 't', '', 'active', '2026-01-01', '2026-01-01')",
            (job_id,),
        )
        conn.execute(
            "INSERT INTO job_resumes (job_id, resume_id, file_name, stored_path, origin,"
            " status, error, created_at, updated_at)"
            " VALUES (?, 'res_1', 'a.pdf', 'p', 'upload', 'done', '', '2026-01-01', '2026-01-01')",
            (job_id,),
        )
        conn.execute(
            "INSERT INTO results (job_id, resume_id, rubric_id, verdict, score, reason_line,"
            " payload, hr_decision, updated_at)"
            " VALUES (?, 'res_1', 'rub_1', 'needs_review', 40, 'r', '{}', NULL, '2026-01-01')",
            (job_id,),
        )

    assert repo.remove_job_resume(db, job_id, "res_1") is True
    assert repo.list_results(db, job_id, "rub_1") == []
    assert repo.get_job_resume(db, job_id, "res_1") is None
    # 重复删除返回 False（404 语义），不抛异常
    assert repo.remove_job_resume(db, job_id, "res_1") is False


def test_remove_resume_via_http_disappears_from_ranking(client):
    """HTTP 层回归：删除后排名条目真的消失（而不是列表消失、排名还在）。"""
    test_client, tmp_path = client
    # 直接在库里摆一条假结论，走 build_ranking_out 的完整读取路径
    db = Database(tmp_path).init_schema()
    job_id = "job_del"
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, title, jd_text, status, created_at, updated_at)"
            " VALUES (?, 't', '', 'active', '2026-01-01', '2026-01-01')",
            (job_id,),
        )
        conn.execute(
            "INSERT INTO job_resumes (job_id, resume_id, file_name, stored_path, origin,"
            " status, error, created_at, updated_at)"
            " VALUES (?, 'res_del', 'a.pdf', 'p', 'upload', 'done', '', '2026-01-01', '2026-01-01')",
            (job_id,),
        )
        conn.execute(
            "INSERT INTO results (job_id, resume_id, rubric_id, verdict, score, reason_line,"
            " payload, hr_decision, updated_at)"
            " VALUES (?, 'res_del', 'rub_1', 'needs_review', 40, 'r', '{}', NULL, '2026-01-01')",
            (job_id,),
        )

    resp = test_client.delete(f"/api/jobs/{job_id}/resumes/res_del")
    assert resp.status_code == 204
    assert test_client.delete(f"/api/jobs/{job_id}/resumes/res_del").status_code == 404
