"""离线脚本模型（provider=scripted）。

它是"没有密钥时看输出形态"的通道，也是本仓库里唯一**不联网就能跑通完整链路**的
模型适配器。因此它必须守住和真实模型一样的两条红线，否则演示出来的"形态"是假的：

1. **每条结论都带原文引用**——quote 必须是 prompt 原文里的逐字片段，
   否则 `core/evidence.py` 会把字段降级、把判定降级为待确认；
2. **找不到就承认未提及**，不许为凑满字段而编造。

这个文件盯的就是这两条，外加几个"规则模型一定会踩"的具体坑。
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from resume_screening.adapters.llm.factory import (
    build_llm_client,
    env_names_for,
    has_api_key,
)
from resume_screening.adapters.llm.scripted import (
    ScriptedLLMClient,
    build_rubric_from_jd,
    extract_fields_from_text,
    judge_dimensions,
)
from resume_screening.core.cache import ResumeCache
from resume_screening.core.extractor import extract_resume
from resume_screening.core.models import ParseResult
from resume_screening.core.quality import assess_text_quality
from resume_screening.core.rubric import Rubric
from resume_screening.core.scoring import evaluate_candidate

# 水印是真实的形态：一长串无中文无空格的编码，且出现在姓名之前。
WATERMARK = "3ad432598e54e3b11Hx92dS6FFVYw4W-UvycWOChl_bTMRFi"

RESUME = f"""{WATERMARK}
{WATERMARK}
张三
求职方向：AI产品经理
上海 ｜ Mobile：138-0013-8000
Email：zhangsan@example.com
教育背景
某某大学
计算机科学与技术            硕士
2019.09-2022.06
实习经历
某某科技有限公司
AI产品经理
2022.07-至今
负责公司自研内容平台的 AI Workflow 设计与迭代，建立结构化评测机制，
通过模型结果对比与问题归因持续优化 Prompt，推动一次生成通过率提升。
搭建质量评测体系，沉淀 Benchmark 与 Bad Case 回流机制，并联动算法与研发
推进上线。参与数据分析，围绕转化漏斗与用户路径持续迭代策略。
"""

JD_MARKDOWN = """## 岗位名称
AI 产品经理（2027 届校招）
## 工作地点
杭州
## 岗位职责
1. 负责 AI 应用类产品的需求调研、竞品分析与产品方案设计，输出 PRD 并推动落地；
2. 与算法、研发、设计协作，把大模型能力转化为可用的产品功能；
## 任职要求
1. 2027 届本科及以上学历应届毕业生，专业不限；
2. 有 AI 产品、AIGC、大模型应用相关的实习或项目经历；
3. 具备较强的逻辑思维与结构化表达能力，能独立完成需求文档与竞品分析报告；
## 加分项
1. 有完整从 0 到 1 的 AI 产品项目经历，并能说明关键决策；
"""

#: 纯文本版：小节名是裸行，不带 `#`。JD 常以 .txt 形式提供。
JD_PLAIN = """岗位职责
1. 负责 AI 应用类产品的需求调研、竞品分析与产品方案设计，输出 PRD 并推动落地；
任职要求
1. 2027 届本科及以上学历应届毕业生，专业不限；
2. 有 AI 产品、AIGC、大模型应用相关的实习或项目经历；
加分项
1. 有完整从 0 到 1 的 AI 产品项目经历，并能说明关键决策；
"""


# ——————————————————— 装配 ———————————————————


def test_factory_builds_scripted_client(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "scripted")

    client = build_llm_client()

    assert isinstance(client, ScriptedLLMClient)
    assert client.name == "scripted"


def test_scripted_needs_no_api_key(monkeypatch) -> None:
    """离线通道不该向用户索要密钥——那会让"没 key 也能看形态"这件事不成立。"""
    monkeypatch.setenv("LLM_PROVIDER", "scripted")

    assert env_names_for() == ()
    assert has_api_key() is True
    assert build_llm_client() is not None


# ——————————————————— 字段抽取 ———————————————————


def test_watermark_before_the_name_does_not_break_name_extraction() -> None:
    """水印出现在姓名之前是真实样本的形态，取姓名必须先剔掉它。"""
    fields = extract_fields_from_text(RESUME)["fields"]

    assert fields["name"]["value"] == "张三"


def test_phone_with_separators_is_matched() -> None:
    """`138-0013-8000` 是 11 位手机号，必须能匹配。

    曾经把正则写成 12 位（`1[3-9]\\d` + 9 组），带分隔符的号码一个都匹配不上。
    """
    fields = extract_fields_from_text(RESUME)["fields"]

    assert fields["contact"]["value"] == "138-0013-8000"


def test_every_extracted_quote_is_a_literal_substring_of_the_source() -> None:
    """反编造：quote 必须在原文里逐字存在，否则核心侧会降级成 unparsable。"""
    fields = extract_fields_from_text(RESUME)["fields"]

    extracted = [f for f in fields.values() if f["status"] == "extracted"]
    assert extracted, "这份样本至少应抽出若干字段"
    for item in extracted:
        quote = item["evidence"]["quote"]
        assert quote in RESUME, f"证据不是原文片段：{quote!r}"


def test_university_line_is_not_mistaken_for_a_company() -> None:
    """「某某大学」含"大学"、真实案例里「西北农林科技大学」含"科技"。

    按公司名关键词找现公司，会把院校行当成公司——这是实测踩到的坑。
    """
    fields = extract_fields_from_text(RESUME)["fields"]

    assert fields["current_company"]["value"] == "某某科技有限公司"
    assert fields["school"]["value"] == "某某大学"


def test_absent_fields_are_reported_as_not_mentioned_not_invented() -> None:
    """原文没有的字段必须是 not_mentioned（不是编一个值，也不是 not_satisfied）。"""
    fields = extract_fields_from_text(RESUME)["fields"]

    for name in ("years_of_experience", "skills"):
        assert fields[name]["status"] == "not_mentioned"
        assert fields[name]["value"] is None
        assert fields[name]["evidence"] is None


def test_text_without_anything_recognisable_yields_no_fabrication() -> None:
    fields = extract_fields_from_text("这里什么都没有，只有一段无关的话。")["fields"]

    assert all(f["status"] == "not_mentioned" for f in fields.values())


# ——————————————————— 评分卡生成 ———————————————————


@pytest.mark.parametrize("jd", [JD_MARKDOWN, JD_PLAIN], ids=["markdown", "plain"])
def test_rubric_is_built_from_both_jd_shapes(jd: str) -> None:
    """JD 可能是 Markdown，也可能是裸行小节的 .txt，两种都要认。

    曾经只认 `#` 开头的标题，纯文本 JD 会生成一张**空的**评分卡。
    """
    rubric = build_rubric_from_jd(jd, "AI产品经理")

    ids = [d["id"] for d in rubric["hard_filters"]]
    assert "min_degree" in ids, rubric
    assert "ai_exp" in ids, rubric
    assert rubric["bonus"], "加分项不应为空"


def test_job_duties_are_not_turned_into_dimensions() -> None:
    """「岗位职责」是"做什么"，不是"招什么人"。把它收进维度会让评分卡失去意义。"""
    rubric = build_rubric_from_jd(JD_MARKDOWN, "AI产品经理")

    names = [d["name"] for d in rubric["hard_filters"] + rubric["bonus"]]
    assert not any("需求调研" in n or "与算法、研发、设计协作" in n for n in names), names


def test_generated_rubric_is_valid_and_passes_validation() -> None:
    """产出的评分卡必须能被 `Rubric` 解析（结构错了后面全跑不动）。"""
    from resume_screening.core.rubric import validate_rubric

    rubric = Rubric.model_validate(
        {"schema_version": "1.0", "version": "t", **build_rubric_from_jd(JD_PLAIN, "AI产品经理")}
    )

    assert rubric.hard_filters
    # 硬门槛没配 rule 只是提示，不该报错
    assert isinstance(validate_rubric(rubric), list)


def test_duplicate_clauses_do_not_collide_on_ids() -> None:
    """同一维度先出现一次进必须项，再来一次要顺延到加分项——否则 id 撞车。"""
    jd = """任职要求
1. 有 AI 产品、大模型应用相关的实习经历；
2. 熟悉主流大模型产品（如 ChatGPT、通义千问等），了解其能力与局限；
"""
    rubric = build_rubric_from_jd(jd, "AI产品经理")

    hard_ids = [d["id"] for d in rubric["hard_filters"]]
    bonus_ids = [d["id"] for d in rubric["bonus"]]
    assert len(hard_ids) == len(set(hard_ids))
    assert len(bonus_ids) == len(set(bonus_ids))
    assert not set(hard_ids) & set(bonus_ids)


# ——————————————————— 维度判定 ———————————————————


def test_dimension_name_in_reason_comes_from_the_prompt() -> None:
    """理由里的维度名必须与评分卡一致。

    脚本模型曾自带一份 id→名字的映射表，一旦与评分卡错位，就会输出
    「命中「数据意识与分析」」而方括号里写着「熟悉主流大模型产品」这种自相矛盾的话。
    """
    prompt_dims = [
        {"id": "b1", "name": "熟悉主流大模型产品"},
        {"id": "b2", "name": "逻辑思维与结构化表达"},
    ]
    payload = judge_dimensions(prompt_dims, "本项目通过 Prompt 与结构化评测提升效果")

    by_id = {r["id"]: r for r in payload["results"]}
    assert "熟悉主流大模型产品" in by_id["b1"]["reason"]
    assert "逻辑思维与结构化表达" in by_id["b2"]["reason"]


def test_unknown_dimension_is_not_mentioned_rather_than_guessed() -> None:
    payload = judge_dimensions([{"id": "手写的维度id", "name": "某个维度"}], "任意文本")

    assert payload["results"][0]["judgement"] == "not_mentioned"


def test_judgement_always_carries_evidence_when_satisfied() -> None:
    payload = judge_dimensions([{"id": "ai_exp", "name": "AI 经验"}], RESUME)

    item = payload["results"][0]
    assert item["judgement"] == "satisfied"
    assert item["evidence"][0]["quote"] in RESUME


# ——————————————————— 端到端：核心侧必须接受它的产出 ———————————————————


def test_full_pipeline_accepts_scripted_output_without_downgrading(tmp_path: Path) -> None:
    """最有价值的一条：让核心的校验逻辑来否决"编造"。

    只要有任何一条判定/字段的 quote 不在原文里，`core/evidence.py` 与
    `core/scoring.py` 就会把它降级，这里断言"一条都没被降级"。
    """
    llm = ScriptedLLMClient()
    parsed = ParseResult(
        resume_id="test-resume",
        source_path="memory://test",
        file_name="张三是简历.pdf",
        raw_text=RESUME,
        quality=assess_text_quality(RESUME),
        pages=1,
    )
    assert parsed.quality.usable, parsed.quality.reasons

    doc = extract_resume(parsed, llm, cache=ResumeCache(tmp_path / "cache"), use_cache=False)

    assert doc.extras.get("evidence_violations") in (None, []), doc.extras.get("evidence_violations")
    assert doc.fields.name.value == "张三"
    assert doc.fields.education.value == "硕士"

    rubric = Rubric.model_validate(
        {"schema_version": "1.0", "version": "t", **build_rubric_from_jd(JD_PLAIN, "AI产品经理")}
    )
    result = evaluate_candidate(doc, rubric, llm)

    assert result.dimensions, "至少要判出维度"
    for dim in result.dimensions:
        assert dim.judgement.value != "unknown", (
            f"{dim.name} 被判为 unknown —— 说明证据没通过校验：{dim.reason}"
        )
        if dim.judgement.value == "satisfied":
            assert dim.evidence, f"{dim.name} 判为满足却没有证据"
            assert dim.evidence[0].quote in RESUME, dim.evidence[0].quote


def test_scripted_run_is_deterministic() -> None:
    """同一份输入必须得到同一份输出，否则它当不了回归基线。"""
    a = extract_fields_from_text(RESUME)
    b = extract_fields_from_text(RESUME)

    assert a == b
