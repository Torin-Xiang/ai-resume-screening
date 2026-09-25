"""评分卡 schema 测试（方案 §5）：三类维度、版本化、口径三件套。"""

from __future__ import annotations

import json

import pytest

from resume_screening.core.rubric import (
    SCHEMA_VERSION,
    Bonus,
    HardFilter,
    RedFlag,
    Rubric,
    validate_rubric,
)


def _sample() -> Rubric:
    return Rubric(
        version="2026-09-26-ai-must",
        job_title="AI 应用开发工程师",
        jd_summary="负责 RAG 与 Agent 应用落地",
        hard_filters=[
            HardFilter(
                id="ai_exp",
                name="AI 项目经验",
                definition="候选人须在项目中实际参与过 AI 能力的设计或交付，而非仅使用成品工具。",
                positive_examples=["独立训练或微调过模型", "搭建过 RAG 或 Agent 流程并上线"],
                negative_examples=["仅调用 ChatGPT 辅助写作", "课程作业级别的 demo"],
            ),
            HardFilter(
                id="min_degree",
                name="本科及以上",
                definition="最高学历须为本科及以上",
                positive_examples=["本科", "硕士"],
                negative_examples=["大专"],
                rule="min_degree",
                params={"min_level": "本科"},
            ),
        ],
        bonus=[
            Bonus(id="bigtech", name="大厂经历", definition="头部互联网公司全职经历", weight=10),
            Bonus(id="edu211", name="211/985 学历", definition="毕业于 211/985 院校", weight=5),
        ],
        red_flags=[
            RedFlag(id="job_hop", name="平均在职时长<1年", definition="平均在职时长不足一年", penalty=8)
        ],
    )


def test_schema_version_default() -> None:
    assert _sample().schema_version == SCHEMA_VERSION


def test_roundtrip_json(tmp_path) -> None:
    r = _sample()
    p = r.to_json(tmp_path / "rubrics" / "demo.json")
    loaded = Rubric.from_json(p)
    assert loaded == r
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["version"] == "2026-09-26-ai-must"


def test_definition_is_required() -> None:
    """方案 §4.6：口径靠 definition 定义，不允许空。"""
    with pytest.raises(Exception):
        HardFilter(id="x", name="空口径", definition="   ")


def test_duplicate_ids_rejected() -> None:
    with pytest.raises(Exception):
        Rubric(
            version="v",
            bonus=[
                Bonus(id="same", name="A", definition="a", weight=1),
                Bonus(id="same", name="B", definition="b", weight=2),
            ],
        )


def test_hard_filter_can_bind_builtin_rule() -> None:
    hf = _sample().hard_filters[1]
    assert hf.rule == "min_degree"
    assert hf.params["min_level"] == "本科"


def test_max_possible_score() -> None:
    assert _sample().max_possible_score() == 15


def test_validate_warns_when_hard_filter_has_no_rule() -> None:
    warns = validate_rubric(_sample())
    assert any("ai_exp" in w and "rule" in w for w in warns)


def test_validate_warns_on_empty_rubric() -> None:
    warns = validate_rubric(Rubric(version="v"))
    assert any("为空" in w for w in warns)


def test_unknown_field_rejected() -> None:
    """extra=forbid：防止手改 JSON 时写错键名而静默生效。"""
    with pytest.raises(Exception):
        Rubric.model_validate({"schema_version": SCHEMA_VERSION, "version": "v", "bonous": []})
