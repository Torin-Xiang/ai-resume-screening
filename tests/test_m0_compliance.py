"""合规红线测试（方案 §4.5）：受保护属性不得进入评分维度。"""

from __future__ import annotations

import pytest

from resume_screening.core.compliance import (
    PROMPT_COMPLIANCE_CLAUSE,
    ComplianceViolation,
    check_text,
    ensure_clean,
    scan_text,
)
from resume_screening.core.rubric import Bonus, HardFilter, RedFlag, Rubric


@pytest.mark.parametrize(
    "text",
    [
        "限男性，年龄 30 岁以下",
        "要求已婚已育，稳定性高",
        "本地户口优先",
        "必须是汉族",
        "政治面貌为中共党员",
        "需提供体检报告，无重大病史",
    ],
)
def test_sensitive_dimensions_are_detected(text: str) -> None:
    hits = scan_text(text)
    assert hits, f"应识别为敏感：{text}"
    with pytest.raises(ComplianceViolation):
        ensure_clean(text)


def test_clean_text_passes() -> None:
    ensure_clean("候选人须独立训练或微调过模型，并有过 RAG 上线经验")


def _expect_compliance_block(fn) -> None:
    """pydantic 会把 ValueError 包装成 ValidationError（其本身也是 ValueError 子类）。"""
    with pytest.raises(ValueError) as exc:
        fn()
    assert "合规红线" in str(exc.value), f"应报合规红线，实际：{exc.value}"


def test_dimension_with_sensitive_definition_rejected() -> None:
    def build() -> None:
        HardFilter(
            id="age",
            name="年龄限制",
            definition="候选人年龄需在 30 岁以下",
            positive_examples=["28 岁"],
            negative_examples=["35 岁"],
        )

    _expect_compliance_block(build)


def test_rubric_rejects_sensitive_bonus_and_redflag() -> None:
    _expect_compliance_block(lambda: Bonus(id="gender", name="性别", definition="男性优先", weight=5))
    _expect_compliance_block(
        lambda: RedFlag(id="married", name="婚育", definition="已婚未育慎选", penalty=5)
    )


def test_rubric_rejects_sensitive_example_hidden_in_examples() -> None:
    """敏感词藏在 examples 里也必须被拦截。"""

    def build() -> None:
        HardFilter(
            id="ai_exp",
            name="AI 项目经验",
            definition="须实际参与 AI 能力交付",
            positive_examples=["30 岁以下候选人独立微调过模型"],
            negative_examples=["仅调用 ChatGPT"],
        )

    _expect_compliance_block(build)


def test_agent_keyword_not_false_positive() -> None:
    """英文词需按词边界匹配：'Agent'/'RAG' 等正常术语不得被误判为年龄维度。"""
    ensure_clean("搭建过 RAG 或 Agent 流程并上线，独立完成微调")
    HardFilter(
        id="ai_exp",
        name="AI 项目经验",
        definition="候选人须实际参与 AI 能力的设计或交付",
        positive_examples=["搭建过 RAG 或 Agent 流程并上线"],
        negative_examples=["课程作业级别的 demo"],
    )


def test_compliant_rubric_loads() -> None:
    r = Rubric(
        version="2026-09-26-test",
        job_title="AI 应用开发工程师",
        hard_filters=[
            HardFilter(
                id="ai_exp",
                name="AI 项目经验",
                definition="候选人须在项目中实际参与过 AI 能力的设计或交付",
                positive_examples=["独立训练或微调过模型"],
                negative_examples=["课程作业级别的 demo"],
            )
        ],
        bonus=[Bonus(id="bigtech", name="大厂经历", definition="头部互联网公司经历", weight=10)],
        red_flags=[RedFlag(id="job_hop", name="平均在职<1年", definition="平均在职时长不足一年", penalty=8)],
    )
    assert r.max_possible_score() == 10


def test_prompt_clause_mentions_all_protected_attributes() -> None:
    for kw in ("性别", "年龄", "婚育", "籍贯", "民族", "健康", "政治面貌"):
        assert kw in PROMPT_COMPLIANCE_CLAUSE


def test_check_text_reports_field() -> None:
    hits = check_text("限男性", field="definition")
    assert hits and hits[0].field == "definition"
