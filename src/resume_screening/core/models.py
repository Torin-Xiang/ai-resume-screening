"""核心中间数据结构（方案 §6.1）。

约定：
1. `ResumeDocument` 尽早定型，后续所有模块都依赖它；**新增字段一律可选**（默认 None/空），
   保证旧缓存 JSON 仍能反序列化，向后兼容。
2. 每个提取字段都带 `evidence`（原文片段）+ `source`（出处定位），并区分
   「未提及 not_mentioned」与「提取失败 unparsable」——未提及不得直接淘汰（方案 §4.3）。
3. 不含任何合规敏感属性（性别/年龄/婚育/籍贯/民族/健康/政治面貌），见 compliance.py。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

SCHEMA_VERSION = "1.0"


class ExtractStatus(str, Enum):
    """字段提取状态。

    - extracted：从原文中找到了依据
    - not_mentioned：简历中**未提及**（≠ 不满足，不得据此淘汰，应标"待确认"）
    - unparsable：原文有相关内容但无法可靠结构化（也应标"待确认"）
    """

    EXTRACTED = "extracted"
    NOT_MENTIONED = "not_mentioned"
    UNPARSABLE = "unparsable"


class Evidence(BaseModel):
    """原文证据：引用片段 + 出处定位。"""

    quote: str = Field(..., description="简历原文片段，必须逐字摘抄，禁止改写或编造")
    source: str = Field("", description="出处定位，如 p1.b3（第 1 页第 3 个文本块）")
    page: Optional[int] = Field(None, description="所在页码，从 1 开始")

    model_config = ConfigDict(extra="forbid")


class FieldValue(BaseModel, Generic[T]):
    """带溯源信息的字段值。"""

    value: Optional[T] = None
    status: ExtractStatus = ExtractStatus.NOT_MENTIONED
    evidence: Optional[Evidence] = None
    note: str = Field("", description="补充说明，如口径存疑处")

    @property
    def is_known(self) -> bool:
        return self.status is ExtractStatus.EXTRACTED and self.value is not None

    @classmethod
    def extracted(cls, value: T, evidence: Evidence | None = None, **kw: Any) -> "FieldValue[T]":
        return cls(value=value, status=ExtractStatus.EXTRACTED, evidence=evidence, **kw)

    @classmethod
    def missing(cls, note: str = "", **kw: Any) -> "FieldValue[T]":
        return cls(value=None, status=ExtractStatus.NOT_MENTIONED, note=note, **kw)


class ParseStatus(str, Enum):
    """解析质量自检结论（方案 §9 风险：扫描件/图片型 PDF）。"""

    OK = "ok"
    SUSPECT = "suspect"  # 可解析但质量存疑，结果需人工留意
    NEEDS_MANUAL = "needs_manual"  # 扫描件/无文本层 → 标记需人工处理，**不送入模型**


class ParseQuality(BaseModel):
    """解析质量自检结果。"""

    status: ParseStatus = ParseStatus.OK
    page_count: int = 0
    char_count: int = 0
    cjk_ratio: float = 0.0  # 中文字符占比，用于识别乱码/扫描件
    reasons: list[str] = Field(default_factory=list)
    is_scanned: bool = Field(False, description="是否为扫描件/图片型 PDF（无有效文本层）")

    @property
    def usable(self) -> bool:
        """是否可送入模型。NEEDS_MANUAL 一律不送入。"""
        return self.status is not ParseStatus.NEEDS_MANUAL


class WorkExperience(BaseModel):
    """工作经历片段（可选字段）。作用：支撑"工作年限"与"跳槽频次"两条规则硬门槛。"""

    company: Optional[str] = None
    title: Optional[str] = None
    start: Optional[str] = None  # 原始文本，如 "2021.07"
    end: Optional[str] = None  # "至今" / "2023.05"
    evidence: Optional[Evidence] = None


class EducationRecord(BaseModel):
    """教育经历片段（可选字段）。"""

    school: Optional[str] = None
    major: Optional[str] = None
    degree: Optional[str] = None  # 博士/硕士/本科/大专
    start: Optional[str] = None
    end: Optional[str] = None
    evidence: Optional[Evidence] = None


class ResumeFields(BaseModel):
    """9 项核心字段（方案 §6.1）：姓名、联系方式、学历、学校、专业、工作年限、现公司、现岗位、技能。"""

    name: FieldValue[str] = Field(default_factory=lambda: FieldValue[str].missing())
    contact: FieldValue[str] = Field(default_factory=lambda: FieldValue[str].missing())
    education: FieldValue[str] = Field(default_factory=lambda: FieldValue[str].missing())
    school: FieldValue[str] = Field(default_factory=lambda: FieldValue[str].missing())
    major: FieldValue[str] = Field(default_factory=lambda: FieldValue[str].missing())
    years_of_experience: FieldValue[float] = Field(
        default_factory=lambda: FieldValue[float].missing()
    )
    current_company: FieldValue[str] = Field(default_factory=lambda: FieldValue[str].missing())
    current_title: FieldValue[str] = Field(default_factory=lambda: FieldValue[str].missing())
    skills: FieldValue[list[str]] = Field(
        default_factory=lambda: FieldValue[list[str]].missing()
    )

    # —— 以下为可选扩展字段，均为空默认值，保证向后兼容 ——
    location: FieldValue[str] = Field(default_factory=lambda: FieldValue[str].missing())
    work_experiences: list[WorkExperience] = Field(default_factory=list)
    education_records: list[EducationRecord] = Field(default_factory=list)

    def core_field_names(self) -> list[str]:
        return [
            "name",
            "contact",
            "education",
            "school",
            "major",
            "years_of_experience",
            "current_company",
            "current_title",
            "skills",
        ]

    def extracted_count(self) -> int:
        return sum(1 for n in self.core_field_names() if getattr(self, n).is_known)


class ResumeDocument(BaseModel):
    """解析 + 提取的最终产物，缓存为中间 JSON；换评分卡重跑时不重复解析。"""

    schema_version: str = SCHEMA_VERSION
    resume_id: str = Field(..., description="简历文件内容 hash（方案 §6.2）")
    source_path: str = Field(..., description="原文件绝对路径，导出时给 HR 回溯用")
    file_name: str = ""
    raw_text: str = Field("", description="按坐标块重排后的纯文本")
    parse_quality: ParseQuality = Field(default_factory=ParseQuality)
    fields: ResumeFields = Field(default_factory=ResumeFields)
    extracted_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    extractor: str = Field("", description="提取器标识，如 llm:qwen-plus / rule:v1")
    extras: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")  # 向后兼容：旧缓存里额外的键不报错


class ParseResult(BaseModel):
    """解析器输出（纯函数：文件路径 → ParseResult，方案 §9）。"""

    resume_id: str
    source_path: str
    file_name: str
    raw_text: str
    quality: ParseQuality
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    pages: int = 0


class Judgement(str, Enum):
    """单条维度的判定结果（方案 §4.3：必须区分「不满足」与「未提及」）。"""

    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"  # 有证据表明不满足 → 可据此淘汰
    NOT_MENTIONED = "not_mentioned"  # 简历未提及 → **不得**据此淘汰，标"待确认"
    UNKNOWN = "unknown"  # 无法判定 → 同样标"待确认"


class DimensionResult(BaseModel):
    """单个评分维度的判定结果。"""

    dimension_id: str
    name: str
    kind: Literal["hard_filter", "bonus", "red_flag"]
    judgement: Judgement
    score: float = 0.0
    reason: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    judged_by: Literal["rule", "llm"] = "llm"

    @property
    def blocks(self) -> bool:
        """硬门槛：只有明确"不满足"才一票否决。"""
        return self.kind == "hard_filter" and self.judgement is Judgement.NOT_SATISFIED


class ScreeningVerdict(str, Enum):
    PASS = "pass"
    REJECT = "reject"
    NEEDS_REVIEW = "needs_review"  # 待确认：含未提及/无法判定的硬门槛，或解析质量存疑


class CandidateResult(BaseModel):
    """单份简历的筛选结果。"""

    resume_id: str
    file_name: str
    source_path: str
    verdict: ScreeningVerdict
    score: float = 0.0
    max_score: float = 0.0
    dimensions: list[DimensionResult] = Field(default_factory=list)
    hard_filter_blocked: bool = False
    parse_status: ParseStatus = ParseStatus.OK
    notes: list[str] = Field(default_factory=list)
    hr_decision: Optional[str] = Field(None, description="HR 人工复核回写：通过/淘汰/待确认")

    def evidence_rows(self) -> str:
        """导出用：把所有证据拼成可读文本。"""
        lines: list[str] = []
        for d in self.dimensions:
            if not d.evidence:
                lines.append(f"[{d.name}] {d.judgement.value}：{d.reason}（无原文引用）")
                continue
            quotes = "；".join(f"“{e.quote}”" for e in d.evidence)
            lines.append(f"[{d.name}] {d.judgement.value}：{d.reason}｜原文：{quotes}")
        return "\n".join(lines)


class RunRecord(BaseModel):
    """一次筛选运行的记录（含评分卡版本，方案 §4.4：不同标准的结果必须可区分）。"""

    run_id: str
    rubric_version: str
    rubric_path: str
    job_title: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    input_dir: str = ""
    results: list[CandidateResult] = Field(default_factory=list)
    llm_call_count: int = 0
    llm_cost_tokens: int = 0
