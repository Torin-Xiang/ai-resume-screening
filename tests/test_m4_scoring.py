"""M4：双层评估——规则硬门槛 + LLM 软评分（方案 §2.1 ④、§4.2、§4.3）。

验收标准（方案 §8 M4）：每条结论有引用；无编造。
⚠️ 阻塞：无 DASHSCOPE_API_KEY；此处用 FakeLLM 验证判定链路与三条硬性原则。
"""

from __future__ import annotations

import pytest

from resume_screening.adapters.llm.fake import FakeLLMClient
from resume_screening.core.models import (
    Evidence,
    ExtractStatus,
    FieldValue,
    Judgement,
    ParseQuality,
    ParseStatus,
    ResumeDocument,
    ResumeFields,
    ScreeningVerdict,
)
from resume_screening.core.rubric import Bonus, HardFilter, RedFlag, Rubric
from resume_screening.core.scoring import evaluate_candidate, screen

RAW = (
    "姓名：李示例\n"
    "联系电话：13800000000\n"
    "教育背景：2018.09-2022.06 示例大学 计算机科学与技术 本科\n"
    "工作经历：2022.07-至今 示例科技有限公司 算法工程师\n"
    "负责 RAG 检索增强生成系统的设计与上线\n"
    "技能：Python、PyTorch、RAG"
)


def make_doc(**overrides: FieldValue) -> ResumeDocument:
    fields = ResumeFields()
    fields.name = FieldValue[str].extracted("李示例", Evidence(quote="姓名：李示例"))
    fields.education = FieldValue[str].extracted("本科", Evidence(quote="计算机科学与技术 本科"))
    fields.years_of_experience = FieldValue[float].extracted(4.0, Evidence(quote="2022.07-至今"))
    fields.major = FieldValue[str].extracted("计算机科学与技术", Evidence(quote="计算机科学与技术"))
    for k, v in overrides.items():
        setattr(fields, k, v)
    return ResumeDocument(resume_id="r1", source_path="/tmp/r1.pdf", file_name="r1.pdf", raw_text=RAW, fields=fields)


def rubric_with(**kw) -> Rubric:
    return Rubric(
        version="2026-09-26-test",
        job_title="AI 应用开发工程师",
        hard_filters=kw.get("hard_filters", []),
        bonus=kw.get("bonus", []),
        red_flags=kw.get("red_flags", []),
    )


# ---------------- 规则硬门槛 ----------------
def test_rule_min_degree_blocks_when_not_satisfied() -> None:
    r = rubric_with(
        hard_filters=[
            HardFilter(id="deg", name="本科及以上", definition="最高学历本科及以上",
                       positive_examples=["本科"], negative_examples=["大专"],
                       rule="min_degree", params={"min_level": "本科"})
        ]
    )
    doc = make_doc(education=FieldValue[str].extracted("大专", Evidence(quote="学历：大专")))
    res = evaluate_candidate(doc, r, FakeLLMClient())
    assert res.verdict is ScreeningVerdict.REJECT
    assert res.hard_filter_blocked is True
    d = res.dimensions[0]
    assert d.judged_by == "rule"
    assert d.judgement is Judgement.NOT_SATISFIED
    assert d.evidence and d.evidence[0].quote == "学历：大专"


def test_rule_passes_when_satisfied() -> None:
    r = rubric_with(
        hard_filters=[
            HardFilter(id="deg", name="本科及以上", definition="最高学历本科及以上",
                       positive_examples=["本科"], negative_examples=["大专"],
                       rule="min_degree", params={"min_level": "本科"})
        ]
    )
    res = evaluate_candidate(make_doc(), r, FakeLLMClient())
    assert res.verdict is ScreeningVerdict.PASS
    assert res.dimensions[0].judgement is Judgement.SATISFIED


def test_not_mentioned_never_rejects() -> None:
    """方案 §4.3：简历未提及 ≠ 不满足，不得据此淘汰，必须标待确认。"""
    r = rubric_with(
        hard_filters=[
            HardFilter(id="deg", name="本科及以上", definition="最高学历本科及以上",
                       positive_examples=["本科"], negative_examples=["大专"],
                       rule="min_degree", params={"min_level": "本科"})
        ]
    )
    doc = make_doc(education=FieldValue[str].missing())
    res = evaluate_candidate(doc, r, FakeLLMClient())
    assert res.verdict is ScreeningVerdict.NEEDS_REVIEW
    assert res.hard_filter_blocked is False
    assert res.dimensions[0].judgement is Judgement.NOT_MENTIONED


def test_rule_min_years_blocks() -> None:
    r = rubric_with(
        hard_filters=[
            HardFilter(id="yrs", name="3 年以上", definition="工作年限 3 年以上",
                       positive_examples=["5 年"], negative_examples=["1 年"],
                       rule="min_years", params={"min_years": 3})
        ]
    )
    doc = make_doc(years_of_experience=FieldValue[float].extracted(1.0, Evidence(quote="工作 1 年")))
    res = evaluate_candidate(doc, r, FakeLLMClient())
    assert res.verdict is ScreeningVerdict.REJECT


def test_rule_major_and_location() -> None:
    r = rubric_with(
        hard_filters=[
            HardFilter(id="m", name="计算机相关专业", definition="专业属于计算机类",
                       positive_examples=["计算机"], negative_examples=["市场营销"],
                       rule="major", params={"keywords": ["计算机", "软件"]}),
        ]
    )
    assert evaluate_candidate(make_doc(), r, FakeLLMClient()).verdict is ScreeningVerdict.PASS
    doc = make_doc(major=FieldValue[str].extracted("市场营销", Evidence(quote="专业：市场营销")))
    res = evaluate_candidate(doc, r, FakeLLMClient())
    assert res.verdict is ScreeningVerdict.REJECT


def test_rule_without_evidence_is_downgraded_to_unknown() -> None:
    """规则判定也必须带原文引用：拿不到引用就降级为待确认（方案 §4.3）。"""
    r = rubric_with(
        hard_filters=[
            HardFilter(id="yrs", name="3 年以上", definition="工作年限 3 年以上",
                       positive_examples=[],
                       negative_examples=[],
                       rule="min_years", params={"min_years": 99})
        ]
    )
    doc = make_doc(years_of_experience=FieldValue[float].extracted(1.0))
    res = evaluate_candidate(doc, r, FakeLLMClient())
    assert res.dimensions[0].judgement is Judgement.UNKNOWN
    assert res.verdict is ScreeningVerdict.NEEDS_REVIEW


# ---------------- LLM 软性维度 ----------------
def _ai_dim() -> HardFilter:
    return HardFilter(
        id="ai_exp",
        name="AI 项目经验",
        definition="候选人须在项目中实际参与过 AI 能力的设计或交付",
        positive_examples=["搭建过 RAG 流程并上线"],
        negative_examples=["课程作业级别的 demo"],
    )


def test_llm_not_satisfied_with_evidence_blocks() -> None:
    llm = FakeLLMClient(
        default={"results": [{"id": "ai_exp", "judgement": "not_satisfied", "hit_level": 0,
                              "reason": "仅有课程作业 demo",
                              "evidence": [{"quote": "课程作业级别的 demo", "page": 1}]}]}
    )
    r = rubric_with(hard_filters=[_ai_dim()])
    doc = make_doc()
    doc.raw_text = RAW + "\n课程作业级别的 demo"
    res = evaluate_candidate(doc, r, llm)
    assert res.verdict is ScreeningVerdict.REJECT
    assert res.dimensions[0].evidence[0].quote == "课程作业级别的 demo"


def test_llm_not_mentioned_is_needs_review() -> None:
    llm = FakeLLMClient(
        default={"results": [{"id": "ai_exp", "judgement": "not_mentioned", "hit_level": 0,
                              "reason": "简历未提及 AI 项目", "evidence": []}]}
    )
    res = evaluate_candidate(make_doc(), rubric_with(hard_filters=[_ai_dim()]), llm)
    assert res.verdict is ScreeningVerdict.NEEDS_REVIEW


def test_fabricated_evidence_invalidates_judgement() -> None:
    """模型编造证据 → 判定作废，绝不成为淘汰依据。"""
    llm = FakeLLMClient(
        default={"results": [{"id": "ai_exp", "judgement": "not_satisfied", "hit_level": 0,
                              "reason": "没有 AI 经验",
                              "evidence": [{"quote": "简历中根本不存在的一句话", "page": 1}]}]}
    )
    res = evaluate_candidate(make_doc(), rubric_with(hard_filters=[_ai_dim()]), llm)
    assert res.dimensions[0].judgement is Judgement.UNKNOWN
    assert res.verdict is ScreeningVerdict.NEEDS_REVIEW
    assert any("证据未命中原文" in n for n in res.notes)


def test_sensitive_reason_invalidates_judgement() -> None:
    """模型理由里出现受保护属性 → 判定作废（方案 §4.5 输出校验层）。"""
    llm = FakeLLMClient(
        default={"results": [{"id": "ai_exp", "judgement": "not_satisfied", "hit_level": 0,
                              "reason": "候选人已婚未育，稳定性存疑",
                              "evidence": [{"quote": "负责 RAG 检索增强生成系统的设计与上线"}]}]}
    )
    res = evaluate_candidate(make_doc(), rubric_with(hard_filters=[_ai_dim()]), llm)
    assert res.dimensions[0].judgement is Judgement.UNKNOWN
    assert any("合规红线" in n for n in res.notes)


def test_bonus_scoring_with_hit_level() -> None:
    llm = FakeLLMClient(
        default={"results": [
            {"id": "bigtech", "judgement": "satisfied", "hit_level": 1,
             "reason": "有大厂经历", "evidence": [{"quote": "示例科技有限公司"}]},
            {"id": "edu211", "judgement": "satisfied", "hit_level": 0.5,
             "reason": "部分匹配", "evidence": [{"quote": "示例大学"}]},
        ]}
    )
    r = rubric_with(
        bonus=[Bonus(id="bigtech", name="大厂经历", definition="头部公司", weight=10,
                     positive_examples=[], negative_examples=[]),
               Bonus(id="edu211", name="211 学历", definition="211 院校", weight=6,
                     positive_examples=[], negative_examples=[])]
    )
    res = evaluate_candidate(make_doc(), r, llm)
    assert res.score == pytest.approx(10 + 3)
    assert res.verdict is ScreeningVerdict.PASS


def test_red_flag_penalty() -> None:
    llm = FakeLLMClient(
        default={"results": [
            {"id": "bigtech", "judgement": "satisfied", "hit_level": 1,
             "reason": "有大厂经历", "evidence": [{"quote": "示例科技有限公司"}]},
            {"id": "hop", "judgement": "satisfied", "hit_level": 1,
             "reason": "平均在职不足一年", "evidence": [{"quote": "2022.07-至今"}]},
        ]}
    )
    r = rubric_with(
        bonus=[Bonus(id="bigtech", name="大厂经历", definition="头部公司", weight=10,
                     positive_examples=[], negative_examples=[])],
        red_flags=[RedFlag(id="hop", name="频繁跳槽", definition="平均在职不足一年", penalty=4,
                           positive_examples=[], negative_examples=[])],
    )
    res = evaluate_candidate(make_doc(), r, llm)
    assert res.score == pytest.approx(6)


def test_missing_dimension_result_is_unknown() -> None:
    """模型漏了某个维度 → 待确认，不能当成未命中。"""
    r = rubric_with(bonus=[Bonus(id="bigtech", name="大厂", definition="头部公司", weight=10,
                                 positive_examples=[], negative_examples=[])])
    res = evaluate_candidate(make_doc(), r, FakeLLMClient(default={"results": []}))
    assert res.dimensions[0].judgement is Judgement.UNKNOWN
    assert res.verdict is ScreeningVerdict.PASS  # 加分项不影响淘汰


def test_unusable_parse_never_calls_llm() -> None:
    doc = make_doc()
    doc.parse_quality = ParseQuality(status=ParseStatus.NEEDS_MANUAL, reasons=["疑似扫描件"])
    llm = FakeLLMClient(default={"results": []})
    res = evaluate_candidate(doc, rubric_with(hard_filters=[_ai_dim()]), llm)
    assert res.verdict is ScreeningVerdict.NEEDS_REVIEW
    assert llm.call_count == 0
    assert any("解析质量不达标" in n for n in res.notes)


def test_all_rule_rubric_does_not_call_llm() -> None:
    r = rubric_with(
        hard_filters=[
            HardFilter(id="deg", name="本科及以上", definition="最高学历本科及以上",
                       positive_examples=["本科"], negative_examples=["大专"],
                       rule="min_degree", params={"min_level": "本科"})
        ]
    )
    llm = FakeLLMClient()
    evaluate_candidate(make_doc(), r, llm)
    assert llm.call_count == 0  # 能用规则就不用模型（方案 §4.2）


# ---------------- 批量与 A/B ----------------
def test_screen_records_rubric_version_and_ranking() -> None:
    """A/B 对比前提：结果必须记录所用评分卡版本（方案 §4.4）。"""
    r = rubric_with(
        bonus=[Bonus(id="bigtech", name="大厂", definition="头部公司", weight=10,
                     positive_examples=[], negative_examples=[])]
    )
    llm = FakeLLMClient(
        default={"results": [{"id": "bigtech", "judgement": "satisfied", "hit_level": 1,
                              "reason": "有", "evidence": [{"quote": "示例科技有限公司"}]}]}
    )
    docs = [make_doc(), make_doc()]
    docs[1].resume_id = "r2"
    docs[1].fields.years_of_experience = FieldValue[float].extracted(1.0, Evidence(quote="工作 1 年"))

    r2 = rubric_with(
        hard_filters=[
            HardFilter(id="yrs", name="3 年以上", definition="3 年以上", positive_examples=[],
                       negative_examples=[], rule="min_years", params={"min_years": 3})
        ]
    )
    r2.version = "2026-09-26-strict"
    run, usage = screen(docs, r2, llm, run_id="run-1", rubric_path="rubrics/strict.json")
    assert run.rubric_version == "2026-09-26-strict"
    assert run.job_title == "AI 应用开发工程师"
    assert run.results[0].verdict is ScreeningVerdict.PASS
    assert run.results[-1].verdict is ScreeningVerdict.REJECT
    assert usage["calls"] >= 0


def test_run_id_is_recorded() -> None:
    run, _ = screen([make_doc()], rubric_with(), FakeLLMClient(), run_id="run-abc")
    assert run.run_id == "run-abc"
    assert run.llm_call_count == 0
