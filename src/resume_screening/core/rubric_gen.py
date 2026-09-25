"""JD → 评分卡草稿（方案 §2.1 ③、§5）。

要点：
1. 生成的是**草稿**：HR 人工编辑后才生效（方案 §3 工作流）。
2. 带业务 `version`：每次筛选结果都要记录所用版本，否则不同标准的结果混在一起无法对比（§4.4）。
3. 合规兜底：模型偶尔会写出"年龄 30 岁以下"这类维度，这里**剔除并记录**，
   不让整份评分卡失败（拦截发生在维度构造层，见 rubric.py）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .compliance import ComplianceViolation
from .llm import LLMClient
from .prompts import RUBRIC_SYSTEM, RUBRIC_USER_TEMPLATE
from .rubric import Bonus, HardFilter, RedFlag, Rubric

DEFAULT_WEIGHT = 5.0
DEFAULT_PENALTY = 5.0
MAX_WEIGHT = 20.0

_BUILTIN_RULES = {"min_degree", "min_years", "location", "major", "job_hop"}


def draft_version() -> str:
    """默认业务版本号：日期 + draft。HR 改口径时应改成有意义的后缀（如 -ai-must）。"""
    return f"{date.today().isoformat()}-draft"


def _clean_rule(value: Any) -> str | None:
    if not value:
        return None
    r = str(value).strip().lower()
    return r if r in _BUILTIN_RULES else None


def _num(value: Any, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, 0.0), MAX_WEIGHT)


def build_rubric_from_payload(
    payload: dict[str, Any],
    *,
    version: str,
    job_title: str = "",
    dropped: list[str] | None = None,
) -> Rubric:
    """把模型输出转成 Rubric；触碰合规红线的维度被剔除并记入 dropped。"""
    dropped = dropped if dropped is not None else []
    data = payload or {}

    hard_filters: list[HardFilter] = []
    for item in data.get("hard_filters") or []:
        try:
            hard_filters.append(
                HardFilter(
                    id=str(item.get("id") or f"hf_{len(hard_filters) + 1}"),
                    name=str(item.get("name") or "未命名必须项"),
                    definition=str(item.get("definition") or ""),
                    positive_examples=[str(x) for x in item.get("positive_examples") or []],
                    negative_examples=[str(x) for x in item.get("negative_examples") or []],
                    rule=_clean_rule(item.get("rule")),
                    params=dict(item.get("params") or {}),
                )
            )
        except (ComplianceViolation, ValueError) as e:
            dropped.append(f"hard_filter/{item.get('id')}: {e}")

    bonus: list[Bonus] = []
    for item in data.get("bonus") or []:
        try:
            bonus.append(
                Bonus(
                    id=str(item.get("id") or f"bonus_{len(bonus) + 1}"),
                    name=str(item.get("name") or "未命名加分项"),
                    definition=str(item.get("definition") or ""),
                    positive_examples=[str(x) for x in item.get("positive_examples") or []],
                    negative_examples=[str(x) for x in item.get("negative_examples") or []],
                    weight=_num(item.get("weight"), DEFAULT_WEIGHT),
                    rule=_clean_rule(item.get("rule")),
                    params=dict(item.get("params") or {}),
                )
            )
        except (ComplianceViolation, ValueError) as e:
            dropped.append(f"bonus/{item.get('id')}: {e}")

    red_flags: list[RedFlag] = []
    for item in data.get("red_flags") or []:
        try:
            red_flags.append(
                RedFlag(
                    id=str(item.get("id") or f"redflag_{len(red_flags) + 1}"),
                    name=str(item.get("name") or "未命名减分项"),
                    definition=str(item.get("definition") or ""),
                    positive_examples=[str(x) for x in item.get("positive_examples") or []],
                    negative_examples=[str(x) for x in item.get("negative_examples") or []],
                    penalty=_num(item.get("penalty"), DEFAULT_PENALTY),
                    rule=_clean_rule(item.get("rule")),
                    params=dict(item.get("params") or {}),
                )
            )
        except (ComplianceViolation, ValueError) as e:
            dropped.append(f"red_flag/{item.get('id')}: {e}")

    rubric = Rubric(
        version=version,
        job_title=job_title or str(data.get("job_title") or ""),
        jd_summary=str(data.get("jd_summary") or ""),
        hard_filters=hard_filters,
        bonus=bonus,
        red_flags=red_flags,
        notes="由 LLM 从 JD 生成的草稿，须经 HR 编辑口径与权重后使用（方案 §3）。",
    )
    if dropped:
        rubric.notes += f"；已剔除 {len(dropped)} 个触碰合规红线的维度"
    return rubric


def generate_rubric(
    jd: str,
    llm: LLMClient,
    *,
    version: str | None = None,
    job_title: str = "",
) -> tuple[Rubric, list[str]]:
    """从 JD 生成评分卡草稿。返回 (rubric, 被剔除的维度说明)。"""
    if not jd or not jd.strip():
        raise ValueError("JD 内容为空：无法生成评分卡")

    payload = llm.complete_json(
        system=RUBRIC_SYSTEM,
        user=RUBRIC_USER_TEMPLATE.format(jd=jd.strip()),
    ).payload
    dropped: list[str] = []
    rubric = build_rubric_from_payload(
        payload, version=version or draft_version(), job_title=job_title, dropped=dropped
    )
    return rubric, dropped
