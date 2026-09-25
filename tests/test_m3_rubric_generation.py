"""M3：JD → 评分卡草稿（生成 + 可编辑 + 合规拦截 + 版本化）（方案 §2.1 ③、§5）。

验收标准（方案 §8 M3）：输出的维度 HR 认可。
⚠️ 阻塞：无 DASHSCOPE_API_KEY，未做真实 JD 生成；此处用 FakeLLM 验证生成链路、
合规剔除与版本化。拿到 key 后用 `rubric new --jd-file jd.txt` 生成并由 HR 评审维度。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from resume_screening.adapters.llm.fake import FakeLLMClient
from resume_screening.cli.main import app
from resume_screening.core.rubric import Bonus, HardFilter, RedFlag, Rubric, validate_rubric
from resume_screening.core.rubric_gen import (
    build_rubric_from_payload,
    draft_version,
    generate_rubric,
)

runner = CliRunner()

_GOOD_PAYLOAD = {
    "job_title": "AI 应用开发工程师",
    "jd_summary": "负责 RAG 与 Agent 应用落地",
    "hard_filters": [
        {
            "id": "ai_exp",
            "name": "AI 项目经验",
            "definition": "候选人须在项目中实际参与过 AI 能力的设计或交付，而非仅使用成品工具。",
            "positive_examples": ["独立训练或微调过模型", "搭建过 RAG 或 Agent 流程并上线"],
            "negative_examples": ["仅调用 ChatGPT 辅助写作", "课程作业级别的 demo"],
            "rule": None,
        },
        {
            "id": "min_degree",
            "name": "本科及以上",
            "definition": "最高学历须为本科及以上",
            "positive_examples": ["本科", "硕士"],
            "negative_examples": ["大专"],
            "rule": "min_degree",
            "params": {"min_level": "本科"},
        },
    ],
    "bonus": [
        {"id": "bigtech", "name": "大厂经历", "definition": "头部互联网公司全职经历",
         "weight": 10, "positive_examples": ["BAT 全职"], "negative_examples": ["外包驻场"]},
        {"id": "edu211", "name": "211/985 学历", "definition": "毕业于 211/985 院校",
         "weight": 5, "positive_examples": ["985 高校"], "negative_examples": ["无"]},
    ],
    "red_flags": [
        {"id": "job_hop", "name": "平均在职时长 < 1 年", "definition": "平均在职时长不足一年",
         "penalty": 8, "rule": "job_hop", "params": {"min_avg_months": 12}},
    ],
}


def test_generate_rubric_from_jd() -> None:
    llm = FakeLLMClient(default=_GOOD_PAYLOAD)
    rubric, dropped = generate_rubric("招 AI 应用开发工程师，要求 RAG 经验", llm)
    assert dropped == []
    assert rubric.job_title == "AI 应用开发工程师"
    assert len(rubric.hard_filters) == 2
    assert rubric.max_possible_score() == 15
    assert rubric.hard_filters[1].rule == "min_degree"
    assert rubric.hard_filters[1].params == {"min_level": "本科"}
    assert "草稿" in rubric.notes


def test_version_defaults_to_dated_draft() -> None:
    rubric, _ = generate_rubric("JD", FakeLLMClient(default=_GOOD_PAYLOAD))
    assert rubric.version == draft_version()
    assert rubric.version.endswith("-draft")
    rubric2, _ = generate_rubric("JD", FakeLLMClient(default=_GOOD_PAYLOAD), version="2026-09-26-ai-must")
    assert rubric2.version == "2026-09-26-ai-must"


def test_sensitive_dimension_generated_by_model_is_dropped() -> None:
    """模型写出"年龄""性别"维度 → 剔除并记账，而不是让整份评分卡失败。"""
    payload = json.loads(json.dumps(_GOOD_PAYLOAD))
    payload["hard_filters"].append(
        {"id": "age", "name": "年龄限制", "definition": "年龄需在 30 岁以下",
         "positive_examples": ["28 岁"], "negative_examples": ["35 岁"]}
    )
    payload["bonus"].append({"id": "gender", "name": "性别", "definition": "男性优先", "weight": 6})
    rubric, dropped = generate_rubric("JD", FakeLLMClient(default=payload))
    assert not any(d.id == "age" for d in rubric.hard_filters)
    assert not any(d.id == "gender" for d in rubric.bonus)
    assert len(dropped) == 2
    assert "合规红线" in dropped[0] or "合规红线" in dropped[1]
    assert "已剔除 2 个" in rubric.notes


def test_empty_jd_rejected() -> None:
    with pytest.raises(ValueError):
        generate_rubric("   ", FakeLLMClient(default=_GOOD_PAYLOAD))


def test_weight_is_clamped() -> None:
    payload = json.loads(json.dumps(_GOOD_PAYLOAD))
    payload["bonus"][0]["weight"] = 999
    payload["bonus"][1]["weight"] = None
    rubric, _ = generate_rubric("JD", FakeLLMClient(default=payload))
    assert rubric.bonus[0].weight == 20.0
    assert rubric.bonus[1].weight == 5.0  # 缺失 → 默认


def test_unknown_rule_id_dropped_to_llm() -> None:
    payload = json.loads(json.dumps(_GOOD_PAYLOAD))
    payload["hard_filters"][0]["rule"] = "unknown_rule"
    rubric, _ = generate_rubric("JD", FakeLLMClient(default=payload))
    assert rubric.hard_filters[0].rule is None


def test_generated_rubric_is_editable_json(tmp_path: Path) -> None:
    """HR 编辑闭环：生成 → 手改 JSON → 重新加载生效。"""
    rubric, _ = generate_rubric("JD", FakeLLMClient(default=_GOOD_PAYLOAD))
    p = rubric.to_json(tmp_path / "r.json")
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["bonus"][0]["weight"] = 3  # HR 调权重
    raw["version"] = "2026-09-26-hr-edited"
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    loaded = Rubric.from_json(p)
    assert loaded.bonus[0].weight == 3
    assert loaded.version == "2026-09-26-hr-edited"


def test_validate_warns_on_dimension_without_examples() -> None:
    r = Rubric(
        version="v",
        job_title="t",
        hard_filters=[HardFilter(id="a", name="A", definition="定义", rule="min_degree")],
    )
    warns = validate_rubric(r)
    assert any("a" in w for w in warns)


# ---------------- CLI ----------------
def test_cli_rubric_new_writes_json(tmp_path: Path, monkeypatch) -> None:
    import resume_screening.cli.rubric_cmd as cmd

    monkeypatch.setattr(cmd, "build_llm_client", lambda **kw: FakeLLMClient(default=_GOOD_PAYLOAD))
    out = tmp_path / "rubrics" / "demo.json"
    result = runner.invoke(app, ["rubric", "new", "--jd", "招 AI 工程师", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    r = Rubric.from_json(out)
    assert r.job_title == "AI 应用开发工程师"


def test_cli_rubric_new_requires_jd() -> None:
    result = runner.invoke(app, ["rubric", "new"])
    assert result.exit_code == 2


def test_cli_rubric_validate_ok(tmp_path: Path) -> None:
    rubric, _ = generate_rubric("JD", FakeLLMClient(default=_GOOD_PAYLOAD))
    p = rubric.to_json(tmp_path / "r.json")
    result = runner.invoke(app, ["rubric", "validate", str(p)])
    assert result.exit_code == 0, result.output
    assert "合法" in result.output


def test_cli_rubric_validate_rejects_sensitive(tmp_path: Path) -> None:
    """手写评分卡加了一个年龄维度 → validate 必须拦下（合规兜底的最后一关）。"""
    p = tmp_path / "bad.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": Rubric.model_fields["schema_version"].default,
                "version": "v1",
                "job_title": "t",
                "bonus": [{"id": "age", "name": "年龄", "definition": "30 岁以下优先", "weight": 5}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["rubric", "validate", str(p)])
    assert result.exit_code == 1
    assert "不合法" in result.output or "合规" in result.output


def test_cli_rubric_show(tmp_path: Path) -> None:
    rubric, _ = generate_rubric("JD", FakeLLMClient(default=_GOOD_PAYLOAD))
    p = rubric.to_json(tmp_path / "r.json")
    result = runner.invoke(app, ["rubric", "show", str(p)])
    assert result.exit_code == 0, result.output
    assert "AI 应用开发工程师" in result.output
    assert "rule=min_degree" in result.output
