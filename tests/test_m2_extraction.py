"""M2：9 字段结构化提取 + 原文溯源 + 缓存（方案 §2.1 ②、§4.3、§6.2）。

验收标准（方案 §8 M2）：字段准确率人工核对 ≥ 90%。
⚠️ 阻塞：无真实脱敏简历样本 + 无 DASHSCOPE_API_KEY，无法做真实准确率核对；
此处用 FakeLLM 验证**提取链路与证据校验**的正确性（含"编造证据必须被拦截"）。
拿到真实样本与 key 后，用 `screen --input fixtures/` 输出核对表做人工准确率统计。
"""

from __future__ import annotations

from pathlib import Path

from resume_screening.adapters.llm.fake import FakeLLMClient
from resume_screening.adapters.parsers.pdf_pymupdf import parse_pdf
from resume_screening.core.cache import ResumeCache
from resume_screening.core.extractor import CORE_FIELDS, extract_resume
from resume_screening.core.models import ExtractStatus, ParseStatus, ResumeDocument


def _payload() -> dict:
    """与 tests/conftest.py 中单栏合成简历对应的"模型输出"。"""
    return {
        "fields": {
            "name": {"value": "李示例", "status": "extracted",
                     "evidence": {"quote": "姓名：李示例", "page": 1}},
            "contact": {"value": "13800000000", "status": "extracted",
                        "evidence": {"quote": "联系电话：13800000000", "page": 1}},
            "education": {"value": "本科", "status": "extracted",
                          "evidence": {"quote": "计算机科学与技术 本科", "page": 1}},
            "school": {"value": "示例大学", "status": "extracted",
                       "evidence": {"quote": "示例大学", "page": 1}},
            "major": {"value": "计算机科学与技术", "status": "extracted",
                      "evidence": {"quote": "计算机科学与技术", "page": 1}},
            "years_of_experience": {"value": 4, "status": "extracted",
                                    "evidence": {"quote": "2022.07-至今", "page": 1}},
            "current_company": {"value": "示例科技有限公司", "status": "extracted",
                                "evidence": {"quote": "示例科技有限公司", "page": 1}},
            "current_title": {"value": "算法工程师", "status": "extracted",
                              "evidence": {"quote": "算法工程师", "page": 1}},
            "skills": {"value": ["Python", "PyTorch", "RAG", "Agent", "Docker"],
                       "status": "extracted",
                       "evidence": {"quote": "技能：Python、PyTorch、RAG、Agent、Docker", "page": 1}},
        },
        "work_experiences": [
            {"company": "示例科技有限公司", "title": "算法工程师", "start": "2022.07", "end": "至今",
             "evidence": {"quote": "2022.07-至今 示例科技有限公司 算法工程师", "page": 1}}
        ],
        "education_records": [
            {"school": "示例大学", "major": "计算机科学与技术", "degree": "本科",
             "evidence": {"quote": "2018.09-2022.06 示例大学 计算机科学与技术 本科", "page": 1}}
        ],
    }


def _extract(pdf: Path, llm: FakeLLMClient, cache_dir: Path):
    pr = parse_pdf(pdf)
    return extract_resume(pr, llm, cache=ResumeCache(cache_dir))


def test_nine_core_fields_extracted(single_column_pdf: Path, tmp_path: Path) -> None:
    llm = FakeLLMClient(default=_payload())
    doc = _extract(single_column_pdf, llm, tmp_path / "cache")
    assert doc.fields.extracted_count() == len(CORE_FIELDS), [
        (n, getattr(doc.fields, n).status) for n in CORE_FIELDS
    ]
    assert doc.fields.name.value == "李示例"
    assert doc.fields.years_of_experience.value == 4.0
    assert doc.fields.skills.value is not None and "PyTorch" in doc.fields.skills.value
    assert doc.extras.get("evidence_violations") is None


def test_every_field_has_evidence_from_source(single_column_pdf: Path, tmp_path: Path) -> None:
    """方案 §4.3：每条结论必须带原文引用。"""
    doc = _extract(single_column_pdf, FakeLLMClient(default=_payload()), tmp_path / "cache")
    for name in CORE_FIELDS:
        fv = getattr(doc.fields, name)
        assert fv.evidence is not None, f"{name} 缺少证据"
        assert fv.evidence.quote in doc.raw_text or fv.evidence.quote.strip() in doc.raw_text


def test_fabricated_evidence_is_downgraded(single_column_pdf: Path, tmp_path: Path) -> None:
    """编造证据：模型给了一个原文中不存在的 quote → 必须降级，不得当作已提取。"""
    payload = _payload()
    payload["fields"]["school"]["evidence"]["quote"] = "清华大学 计算机系 硕士"
    doc = _extract(single_column_pdf, FakeLLMClient(default=payload), tmp_path / "cache")

    assert doc.fields.school.status is not ExtractStatus.EXTRACTED
    assert doc.fields.school.value is None
    assert doc.fields.school.evidence is None
    assert any("school" in v for v in doc.extras["evidence_violations"])


def test_claimed_extraction_without_quote_is_downgraded(single_column_pdf: Path, tmp_path: Path) -> None:
    payload = _payload()
    payload["fields"]["major"].pop("evidence")
    doc = _extract(single_column_pdf, FakeLLMClient(default=payload), tmp_path / "cache")
    assert doc.fields.major.status is ExtractStatus.UNPARSABLE
    assert doc.fields.major.value is None


def test_not_mentioned_is_not_not_satisfied(tmp_path: Path) -> None:
    """未提及 ≠ 不满足：状态必须是 not_mentioned 且 value 为 None，供下游标"待确认"。"""
    from tests.conftest import write_pdf

    pdf = write_pdf(tmp_path / "partial.pdf", [(50, 60, "姓名：王示例"), (50, 90, "技能：Python")])
    payload = {"fields": {"name": {"value": "王示例", "status": "extracted",
                                   "evidence": {"quote": "姓名：王示例", "page": 1}}}}
    doc = _extract(pdf, FakeLLMClient(default=payload), tmp_path / "cache")
    assert doc.fields.name.is_known
    for name in ("education", "school", "major", "current_company", "current_title"):
        fv = getattr(doc.fields, name)
        assert fv.status is ExtractStatus.NOT_MENTIONED
        assert fv.value is None


def test_contact_rule_fallback(tmp_path: Path) -> None:
    """联系方式用规则兜底（方案 §4.2：能用规则就不用 LLM）。"""
    from tests.conftest import write_pdf

    pdf = write_pdf(tmp_path / "c.pdf", [(50, 60, "姓名：王示例"), (50, 90, "手机：13900001111")])
    doc = _extract(pdf, FakeLLMClient(default={"fields": {}}), tmp_path / "cache")
    assert doc.fields.contact.is_known
    assert doc.fields.contact.value == "13900001111"
    assert "规则" in doc.fields.contact.note


def test_scanned_pdf_never_sent_to_llm(scanned_pdf: Path, tmp_path: Path) -> None:
    """扫描件：标记 needs_manual，且不调用模型（方案 §9）。"""
    llm = FakeLLMClient(default=_payload())
    doc = _extract(scanned_pdf, llm, tmp_path / "cache")
    assert doc.parse_quality.status is ParseStatus.NEEDS_MANUAL
    assert llm.call_count == 0
    assert doc.fields.extracted_count() == 0
    assert "skipped_reason" in doc.extras


def test_cache_avoids_repeat_llm_calls(single_column_pdf: Path, tmp_path: Path) -> None:
    """换评分卡重跑时不重复解析提取：第二次命中缓存，零 LLM 调用（方案 §4.4）。"""
    cache_dir = tmp_path / "cache"
    pr = parse_pdf(single_column_pdf)
    llm = FakeLLMClient(default=_payload())
    cache = ResumeCache(cache_dir)
    d1 = extract_resume(pr, llm, cache=cache)
    assert llm.call_count == 1
    d2 = extract_resume(pr, llm, cache=cache)
    assert llm.call_count == 1
    assert d1.fields == d2.fields
    assert (cache_dir / f"{pr.resume_id}.json").exists()


def test_cache_key_is_content_not_path(single_column_pdf: Path, tmp_path: Path) -> None:
    """同一份简历复制到别处，resume_id 不变 → 仍命中缓存（用真实复制，字节完全一致）。"""
    import shutil

    dest_dir = tmp_path / "nested"
    dest_dir.mkdir()
    copy = dest_dir / "copy.pdf"
    shutil.copy(single_column_pdf, copy)
    cache = ResumeCache(tmp_path / "cache")
    llm = FakeLLMClient(default=_payload())
    extract_resume(parse_pdf(single_column_pdf), llm, cache=cache)
    assert llm.call_count == 1
    extract_resume(parse_pdf(copy), llm, cache=cache)
    assert llm.call_count == 1, "内容相同不应重复调用模型"


def test_cache_corrupted_file_falls_back(single_column_pdf: Path, tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    pr = parse_pdf(single_column_pdf)
    (cache_dir / f"{pr.resume_id}.json").write_text("{ broken json", encoding="utf-8")
    cache = ResumeCache(cache_dir)
    assert cache.hit(pr.resume_id) is None
    doc = extract_resume(pr, FakeLLMClient(default=_payload()), cache=cache)
    assert doc.fields.name.is_known


def test_work_and_education_records_parsed(single_column_pdf: Path, tmp_path: Path) -> None:
    doc = _extract(single_column_pdf, FakeLLMClient(default=_payload()), tmp_path / "cache")
    assert len(doc.fields.work_experiences) == 1
    assert doc.fields.work_experiences[0].company == "示例科技有限公司"
    assert len(doc.fields.education_records) == 1
    assert doc.fields.education_records[0].degree == "本科"


# ——————————————————— 缓存失效语义 ———————————————————


def test_forced_recompute_still_writes_back_to_cache(
    single_column_pdf: Path, tmp_path: Path
) -> None:
    """`use_cache=False` 是"忽略旧缓存、强制重算"，**不是"不写缓存"**。

    曾经的实现把写入也放在 `if use_cache:` 里：`--no-cache` 跑完，新结果只活在
    这次运行的进程里，报告与界面读缓存拿到的还是旧值——表现成"重跑了一遍，
    什么都没变"，而且完全看不出是缓存干的。
    """
    cache_dir = tmp_path / "cache"

    doc = extract_resume(
        parse_pdf(single_column_pdf),
        FakeLLMClient(default=_payload()),
        cache=ResumeCache(cache_dir),
        use_cache=False,
    )

    assert (cache_dir / f"{doc.resume_id}.json").exists(), "强制重算的结果没有写回缓存"


def test_extractor_tag_records_which_model_produced_the_result(
    single_column_pdf: Path, tmp_path: Path
) -> None:
    """缓存键只有文件内容 hash，模型标识必须写进 extractor 才有得比对。"""
    llm = FakeLLMClient(default=_payload())

    doc = _extract(single_column_pdf, llm, tmp_path / "cache")

    assert doc.extractor.endswith(f":{llm.name}"), doc.extractor


def test_cache_hit_is_refused_when_the_extractor_tag_differs(tmp_path: Path) -> None:
    """换模型、或升级提取逻辑后必须重算，不能静默复用旧结果。"""
    cache = ResumeCache(tmp_path / "cache")
    cache.put(
        ResumeDocument(
            resume_id="abc123",
            source_path="/tmp/x.pdf",
            extractor="extract-v1:model-a",
        )
    )

    assert cache.hit("abc123", expect_extractor="extract-v1:model-a") is not None
    assert cache.hit("abc123", expect_extractor="extract-v1:model-b") is None
    assert cache.hit("abc123", expect_extractor="extract-v2:model-a") is None
    # 不传期望值时不校验，只回答"有没有这份缓存"
    assert cache.hit("abc123") is not None


def test_changing_the_model_forces_re_extraction(single_column_pdf: Path, tmp_path: Path) -> None:
    """端到端：同一模型第二次不调模型；换一个模型立刻重算。"""
    cache = ResumeCache(tmp_path / "cache")
    pr = parse_pdf(single_column_pdf)

    first = FakeLLMClient(default=_payload())
    extract_resume(pr, first, cache=cache)
    extract_resume(pr, first, cache=cache)
    assert first.call_count == 1

    other = FakeLLMClient(default=_payload())
    other.name = "another-model"
    extract_resume(pr, other, cache=cache)
    assert other.call_count == 1, "换了模型却命中了旧缓存"


def test_both_cache_implementations_accept_the_same_hit_arguments(tmp_path: Path) -> None:
    """两套缓存实现（JSON 文件版 / SQLite 版）必须保持同一套 `hit` 接口。

    Web 端注入的是 SQLite 版。曾经只给文件版加了 `expect_extractor` 校验、
    忘了 SQLite 版，于是服务端每次处理都抛 `TypeError`，异常被后台任务吞掉——
    **上传后一份简历都出不来**，而单跑 CLI 完全正常（CLI 用文件版）。
    接口对齐不能只靠"改的时候记得两处一起动"的自觉，要有测试盯着。
    """
    from resume_screening.store.db import Database
    from resume_screening.store.resume_cache import SqliteResumeCache

    doc = ResumeDocument(
        resume_id="rid",
        source_path="/tmp/x.pdf",
        extractor="extract-v1:model-a",
    )
    caches = [
        ResumeCache(tmp_path / "cache"),
        SqliteResumeCache(Database(tmp_path / "d").init_schema()),
    ]

    for cache in caches:
        name = type(cache).__name__
        cache.put(doc)
        assert cache.hit("rid", expect_extractor="extract-v1:model-a") is not None, name
        assert cache.hit("rid", expect_extractor="extract-v1:model-b") is None, name
        assert cache.hit("rid") is not None, name
