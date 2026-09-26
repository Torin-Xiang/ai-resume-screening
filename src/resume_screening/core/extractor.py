"""结构化提取：9 项核心字段 + 每项原文溯源（方案 §2.1 ②、§4.3）。

关键约束：
1. 每条字段都要有 evidence；**evidence 必须能在原文中逐字找到**，否则视为编造并降级为
   「未提及/无法判定」，绝不写成"已提取"。
2. 解析质量 unusable（扫描件）→ 不送入模型，直接返回待人工处理的文档。
3. 命中缓存则不调用模型（换评分卡重跑时不花这份钱）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .evidence import quote_in_text
from .llm import LLMClient, MeteredLLM
from .models import (
    EducationRecord,
    Evidence,
    ExtractStatus,
    FieldValue,
    ParseResult,
    ParseStatus,
    ResumeDocument,
    ResumeFields,
    WorkExperience,
)
from .prompts import EXTRACT_JSON_SCHEMA_HINT, EXTRACT_SYSTEM, EXTRACT_USER_TEMPLATE

EXTRACTOR_VERSION = "extract-v1"

_PHONE = re.compile(r"1[3-9]\d{9}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# 9 项核心字段（方案 §6.1）
CORE_FIELDS = (
    "name",
    "contact",
    "education",
    "school",
    "major",
    "years_of_experience",
    "current_company",
    "current_title",
    "skills",
)


def _to_status(v: Any) -> ExtractStatus:
    try:
        return ExtractStatus(str(v).lower())
    except ValueError:
        return ExtractStatus.NOT_MENTIONED


def _coerce(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field == "years_of_experience":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if field == "skills":
        if isinstance(value, list):
            return [str(x) for x in value]
        if isinstance(value, str):
            return [s.strip() for s in re.split(r"[,，、/|]", value) if s.strip()]
        return None
    return str(value).strip() or None


def build_fields(
    payload: dict[str, Any],
    raw_text: str,
    *,
    violations: list[str] | None = None,
) -> ResumeFields:
    """把模型输出转成带溯源的字段；证据校验不过的字段一律降级。"""
    violations = violations if violations is not None else []
    raw_fields = (payload or {}).get("fields", {}) or {}
    fields = ResumeFields()

    for name in CORE_FIELDS + ("location",):
        item = raw_fields.get(name) or {}
        value = _coerce(name, item.get("value"))
        status = _to_status(item.get("status", "not_mentioned"))
        evidence: Evidence | None = None
        ev = item.get("evidence") or {}
        quote = (ev.get("quote") or "").strip() if isinstance(ev, dict) else ""

        if status is ExtractStatus.EXTRACTED and not value:
            status = ExtractStatus.NOT_MENTIONED

        if status is ExtractStatus.EXTRACTED and quote:
            if quote_in_text(quote, raw_text):
                evidence = Evidence(
                    quote=quote,
                    source=f"p{ev.get('page', 1)}" if ev.get("page") else "",
                    page=ev.get("page"),
                )
            else:
                # 编造/改写证据 —— 方案 §4.3：引不出原文就承认不知道
                violations.append(f"{name}: 证据未命中原文，已降级为 unparsable（quote={quote!r}）")
                status = ExtractStatus.UNPARSABLE
        elif status is ExtractStatus.EXTRACTED and not quote:
            violations.append(f"{name}: 声称已提取但无原文引用，已降级为 unparsable")
            status = ExtractStatus.UNPARSABLE

        setattr(
            fields,
            name,
            FieldValue(value=value if status is ExtractStatus.EXTRACTED else None,
                       status=status,
                       evidence=evidence),
        )

    fields.work_experiences = [
        _build_work_experience(w, raw_text) for w in (payload or {}).get("work_experiences", []) or []
    ]
    fields.education_records = [
        _build_education_record(e, raw_text)
        for e in (payload or {}).get("education_records", []) or []
    ]
    _rule_fallback_contact(fields, raw_text)
    return fields


def _build_work_experience(item: dict[str, Any], raw_text: str) -> WorkExperience:
    ev = item.get("evidence") or {}
    quote = (ev.get("quote") or "").strip() if isinstance(ev, dict) else ""
    evidence = (
        Evidence(quote=quote, page=ev.get("page")) if quote and quote_in_text(quote, raw_text) else None
    )
    return WorkExperience(
        company=item.get("company"),
        title=item.get("title"),
        start=item.get("start"),
        end=item.get("end"),
        evidence=evidence,
    )


def _build_education_record(item: dict[str, Any], raw_text: str) -> EducationRecord:
    ev = item.get("evidence") or {}
    quote = (ev.get("quote") or "").strip() if isinstance(ev, dict) else ""
    evidence = (
        Evidence(quote=quote, page=ev.get("page")) if quote and quote_in_text(quote, raw_text) else None
    )
    return EducationRecord(
        school=item.get("school"),
        major=item.get("major"),
        degree=item.get("degree"),
        start=item.get("start"),
        end=item.get("end"),
        evidence=evidence,
    )


def _rule_fallback_contact(fields: ResumeFields, raw_text: str) -> None:
    """联系方式这类高确定性字段用规则兜底（方案 §4.2：能用规则就用规则）。"""
    if fields.contact.is_known:
        return
    m = _PHONE.search(raw_text) or _EMAIL.search(raw_text)
    if not m:
        return
    quote = m.group(0)
    # 取包含该号码的一小段原文，便于 HR 核对
    start = max(0, m.start() - 12)
    snippet = raw_text[start : m.end() + 12].strip()
    fields.contact = FieldValue[str].extracted(
        quote,
        Evidence(quote=snippet if quote_in_text(snippet, raw_text) else quote),
        note="由规则正则兜底抽取",
    )


def extract_resume(
    parse_result: ParseResult,
    llm: LLMClient,
    *,
    cache: "ResumeCache | None" = None,
    use_cache: bool = True,
) -> ResumeDocument:
    """解析结果 → ResumeDocument。命中缓存不调模型；扫描件不送入模型。

    `use_cache=False` 的含义是**忽略已有缓存、强制重算**，而不是"不写缓存"：
    重算结果照常写回。否则 `--no-cache` 跑出来的新结果只活在这一次运行的进程里，
    报告与界面读缓存拿到的还是旧值，表现成"重跑了一遍，什么都没变"。
    """
    from .cache import ResumeCache  # 局部导入：避免循环依赖，保持模块可单独使用

    if cache is None:
        cache = ResumeCache()

    # 缓存可信的条件：原文没变 + 提取逻辑没升级 + 同一个模型。
    # 键（文件内容 hash）只覆盖第一条，后两条靠这个 tag 比对。
    extractor_tag = f"{EXTRACTOR_VERSION}:{getattr(llm, 'name', 'unknown')}"
    if use_cache:
        cached = cache.hit(parse_result.resume_id, expect_extractor=extractor_tag)
        if cached is not None:
            cached.source_path = parse_result.source_path  # 路径可能变，内容未变
            return cached

    doc = ResumeDocument(
        resume_id=parse_result.resume_id,
        source_path=parse_result.source_path,
        file_name=parse_result.file_name,
        raw_text=parse_result.raw_text,
        parse_quality=parse_result.quality,
        extractor=extractor_tag,
    )

    if not parse_result.quality.usable:
        doc.extras["skipped_reason"] = "解析质量不达标（方案 §9：扫描件/无文本层不送入模型）"
        doc.extractor = f"{EXTRACTOR_VERSION}:skipped"
        cache.put(doc)
        return doc

    payload = llm.complete_json(
        system=EXTRACT_SYSTEM,
        user=EXTRACT_USER_TEMPLATE.format(
            pages=parse_result.pages or parse_result.quality.page_count or 1,
            chars=parse_result.quality.char_count,
            text=parse_result.raw_text,
        ),
        json_schema=EXTRACT_JSON_SCHEMA_HINT,
    ).payload

    violations: list[str] = []
    doc.fields = build_fields(payload, parse_result.raw_text, violations=violations)
    if violations:
        doc.extras["evidence_violations"] = violations
    cache.put(doc)
    return doc


def extract_many(
    parse_results: list[ParseResult],
    llm: LLMClient,
    *,
    cache_dir: str | Path = "data/cache",
    use_cache: bool = True,
) -> tuple[list[ResumeDocument], dict[str, int]]:
    """批量提取，返回 (文档列表, LLM 计量快照)。"""
    from .cache import ResumeCache

    cache = ResumeCache(cache_dir)
    metered = MeteredLLM(llm)
    docs = [extract_resume(pr, metered, cache=cache, use_cache=use_cache) for pr in parse_results]
    return docs, metered.snapshot()
