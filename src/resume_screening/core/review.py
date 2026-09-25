"""HR 判定回写（方案 §2.1 ⑤）。

回写数据是 v2 唯一改进依据（校准维度权重）。MVP 只做本地 JSON 落盘，不做权重自动学习（§4.4）。
"""

from __future__ import annotations

from typing import Any

from .models import RunRecord

VALID_DECISIONS = ("通过", "淘汰", "待确认")


def apply_decisions(
    run: RunRecord, decisions: dict[str, dict[str, Any]]
) -> tuple[int, list[str], list[str]]:
    """把 HR 判定写回结果。返回 (更新条数, 未知 resume_id, 非法判定值)。"""
    updated = 0
    unknown: list[str] = []
    invalid: list[str] = []

    for resume_id, payload in decisions.items():
        target = next((r for r in run.results if r.resume_id == resume_id), None)
        if target is None:
            unknown.append(resume_id)
            continue
        decision = str(payload.get("hr_decision") or "").strip()
        note = str(payload.get("hr_note") or "").strip()
        if decision and decision not in VALID_DECISIONS:
            invalid.append(f"{resume_id}: {decision}")
            continue
        if decision:
            target.hr_decision = decision
        if note:
            target.notes.append(f"HR 备注：{note}")
        updated += 1
    return updated, unknown, invalid


def review_summary(run: RunRecord) -> dict[str, int]:
    from collections import Counter

    c = Counter(r.hr_decision or "未复核" for r in run.results)
    return dict(c)
