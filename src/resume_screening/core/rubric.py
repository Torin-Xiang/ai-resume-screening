"""评分卡 schema 与读写（方案 §5、§4.4）。

设计要点：
1. 评分卡是**外置 JSON 配置**，带 `schema_version`（结构版本）与 `version`（业务版本）；
   业务口径变化时只改 JSON，代码零改动。
2. 每个维度必须有 `definition` + `positive_examples` + `negative_examples`
   （方案 §4.6：判定口径靠自然语言定义，不靠布尔规则）。
3. hard_filters 支持两种判定方式：
   - 指定 `rule`（内置规则 id，如 min_degree / min_years / location / major / job_hop）
     → 由规则引擎判定，成本 0、准确率 100%、完全可解释（方案 §4.2 优先路径）；
   - 未指定 `rule` → 交给 LLM 做软性判定，且必须给原文证据、区分未提及（方案 §4.3）。
4. 加载时做合规检查（方案 §4.5）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .compliance import ComplianceViolation, check_text

SCHEMA_VERSION = "1.0"


class DimensionBase(BaseModel):
    """三类维度共用字段。"""

    id: str = Field(..., description="维度唯一 id，用于结果对齐与 A/B 对比")
    name: str
    definition: str = Field(..., description="判定口径的自然语言定义（方案 §4.6）")
    positive_examples: list[str] = Field(default_factory=list)
    negative_examples: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")

    @field_validator("definition")
    @classmethod
    def _definition_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("definition 不得为空：口径不明会导致同一份简历每次判定结果不同")
        return v.strip()

    @model_validator(mode="after")
    def _compliance_guard(self) -> "DimensionBase":
        """合规红线：维度一旦被单独构造就立即拦截，不等到组进评分卡。"""
        for field, text in self.declared_texts():
            hits = check_text(text, field=f"{self.id}.{field}")
            if hits:
                cats = "、".join(sorted({h.category for h in hits}))
                raise ComplianceViolation(
                    f"合规红线：维度 {self.id}（{self.name}）的 {field} 命中受保护属性（{cats}）。"
                    "依据方案 §4.5，性别/年龄/婚育/籍贯/民族/健康/政治面貌不得进入评分维度。"
                )
        return self

    def declared_texts(self) -> list[tuple[str, str]]:
        """供合规检查的文本片段。"""
        out = [("name", self.name), ("definition", self.definition)]
        for i, e in enumerate(self.positive_examples):
            out.append((f"positive_examples[{i}]", e))
        for i, e in enumerate(self.negative_examples):
            out.append((f"negative_examples[{i}]", e))
        return out


class HardFilter(DimensionBase):
    """必须项：判定为「不满足」即一票否决；「未提及」不得淘汰。"""

    kind: Literal["hard_filter"] = "hard_filter"
    rule: Optional[str] = Field(
        None,
        description="内置规则 id（min_degree/min_years/location/major/job_hop）。"
        "有 rule 走规则引擎；无 rule 走 LLM 软性判定（必须带证据）",
    )
    params: dict[str, Any] = Field(default_factory=dict)


class Bonus(DimensionBase):
    """加分项：权重 × 命中度，累加。"""

    kind: Literal["bonus"] = "bonus"
    weight: float = Field(..., ge=0)
    rule: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)


class RedFlag(DimensionBase):
    """减分项：命中则扣分。"""

    kind: Literal["red_flag"] = "red_flag"
    penalty: float = Field(..., ge=0)
    rule: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)


class Rubric(BaseModel):
    """评分卡。"""

    schema_version: str = SCHEMA_VERSION
    version: str = Field(..., description="业务版本号，如 2026-09-26-ai-must；每次筛选结果必须记录")
    job_title: str = ""
    jd_summary: str = ""
    hard_filters: list[HardFilter] = Field(default_factory=list)
    bonus: list[Bonus] = Field(default_factory=list)
    red_flags: list[RedFlag] = Field(default_factory=list)
    notes: str = ""

    model_config = ConfigDict(extra="forbid")

    # ---------- 校验：合规 + id 唯一 ----------
    @field_validator("hard_filters", "bonus", "red_flags")
    @classmethod
    def _ids_unique_and_still_compliant(cls, v: list[Any]) -> list[Any]:
        """维度自身合规已在 DimensionBase 拦截；这里兜底并校验 id 唯一（结果对齐与 A/B 对比依赖 id）。"""
        seen: set[str] = set()
        for dim in v:
            if dim.id in seen:
                raise ValueError(f"维度 id 重复：{dim.id}（结果对齐与 A/B 对比依赖 id 唯一）")
            seen.add(dim.id)
            for field, text in dim.declared_texts():
                if check_text(text, field=f"{dim.id}.{field}"):
                    raise ComplianceViolation(
                        f"合规红线：维度 {dim.id}（{dim.name}）含受保护属性，不得进入评分维度（方案 §4.5）。"
                    )
        return v

    def all_dimensions(self) -> list[DimensionBase | HardFilter | Bonus | RedFlag]:
        return [*self.hard_filters, *self.bonus, *self.red_flags]

    def max_possible_score(self) -> float:
        return sum(b.weight for b in self.bonus)

    def to_json(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return p

    @classmethod
    def from_json(cls, path: str | Path) -> "Rubric":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)


class RubricValidationError(ValueError):
    pass


def validate_rubric(rubric: Rubric) -> list[str]:
    """返回可读性警告（不抛错）：如硬门槛缺 rule 时提示口径需 LLM 判定。"""
    warnings: list[str] = []
    for hf in rubric.hard_filters:
        if hf.rule is None:
            warnings.append(
                f"硬门槛 {hf.id} 未指定内置 rule，将由 LLM 判定；"
                "建议能规则化的（学历/年限/地点/专业/跳槽频次）尽量指定 rule 以保证 100% 可解释。"
            )
        if not hf.positive_examples or not hf.negative_examples:
            warnings.append(f"硬门槛 {hf.id} 缺少正例或反例，口径可能不稳定（方案 §4.6）。")
    for b in rubric.bonus:
        if not b.positive_examples and not b.negative_examples:
            warnings.append(f"加分项 {b.id} 缺少正反例，命中度判定可能漂移。")
    if not rubric.hard_filters and not rubric.bonus and not rubric.red_flags:
        warnings.append("评分卡为空：所有候选人都会得到 0 分且不淘汰，请确认是否漏配。")
    return warnings
