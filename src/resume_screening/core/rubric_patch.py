"""自然语言改标准：变更提案的结构与"应用"（技术方案 §7 第①③段）。

**为什么不直接让模型输出一份新的评分卡 JSON**（技术方案 §7 明确不做的事）：
那样无法向用户解释"改了什么"，也无法计算影响，更无法支持"部分采纳"。
所以模型只被允许输出一份**结构化的变更清单**，每条都要说清改前、改后、为什么。

这里只有纯函数：解析模型输出 → 校验 → 应用到评分卡。不调用模型、不碰数据库，
因此可以单独测，也保证了"影响计算不调模型"这条性能前提（技术方案 §7）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .compliance import check_text
from .rubric import Rubric

KIND_TO_FIELD: dict[str, str] = {
    "hard_filter": "hard_filters",
    "bonus": "bonus",
    "red_flag": "red_flags",
}
FIELD_TO_KIND: dict[str, str] = {v: k for k, v in KIND_TO_FIELD.items()}


class RubricPatchError(ValueError):
    """变更清单不合法（模型乱输出、或与当前标准对不上）。"""


class RubricPatch(BaseModel):
    """一条变更。

    `before` / `after` 存**维度 JSON**而不是字段级的 diff：
    维度的字段少且必须整块看（口径 + 正反例 + 权重是一体的），
    字段级 diff 会让"改了什么"变得难读，反而增加确认成本（产品方案 §6.4）。
    """

    op: Literal["add", "remove", "modify"]
    kind: Literal["hard_filter", "bonus", "red_flag"]
    target_id: str = Field(..., description="被改的维度 id；add 时为建议的新 id")
    before: dict[str, Any] | None = Field(None, description="改前维度（remove/modify 必填）")
    after: dict[str, Any] | None = Field(None, description="改后维度（add/modify 必填）")
    reason: str = Field(..., description="为什么这么改，一句话，给 HR 看")

    model_config = ConfigDict(extra="forbid")

    @property
    def field(self) -> str:
        return KIND_TO_FIELD[self.kind]


class Proposal(BaseModel):
    """一次自然语言改标准请求的完整产出。"""

    patches: list[RubricPatch] = Field(default_factory=list)
    summary: str = Field("", description="一句话说明这次改了什么")
    refused_reason: str = Field(
        "",
        description="非空表示该要求无法执行（通常是触碰合规红线），HR 需要看到理由",
    )

    model_config = ConfigDict(extra="forbid")

    @property
    def refused(self) -> bool:
        return bool(self.refused_reason.strip())

    def select(self, indices: list[int] | None) -> "Proposal":
        """按索引取子集，用于「部分采纳」（产品方案 §6.4 的第二个选项）。"""
        if indices is None:
            return self
        chosen = []
        for i in indices:
            if not 0 <= i < len(self.patches):
                raise RubricPatchError(f"变更序号超出范围：{i}（共 {len(self.patches)} 条）")
            chosen.append(self.patches[i])
        return Proposal(patches=chosen, summary=self.summary, refused_reason=self.refused_reason)


def parse_proposal(payload: Any) -> Proposal:
    """把模型输出解析成 Proposal，并做**输出侧合规拦截**（技术方案 §7）。

    合规要两侧各拦一次：输入侧拦不住模型的自由发挥，输出侧拦不住用户的迂回表达。
    这里是输出侧——模型给出的维度名称、口径、正反例、甚至改动理由都过一遍。
    """
    try:
        proposal = Proposal.model_validate(payload)
    except ValidationError as exc:
        raise RubricPatchError(f"变更清单格式不合法：{exc}") from exc

    for patch in proposal.patches:
        if patch.op == "add" and patch.after is None:
            raise RubricPatchError(f"变更 {patch.target_id}：add 必须给出 after")
        if patch.op in ("remove", "modify") and patch.before is None:
            raise RubricPatchError(f"变更 {patch.target_id}：{patch.op} 必须给出 before")
        if patch.op == "modify" and patch.after is None:
            raise RubricPatchError(f"变更 {patch.target_id}：modify 必须给出 after")

        # 改动理由也会展示给 HR，同样不能夹带受保护属性
        hits = check_text(patch.reason or "", field=f"{patch.target_id}.reason")
        if hits:
            cats = "、".join(sorted({h.category for h in hits}))
            raise RubricPatchError(
                f"变更 {patch.target_id} 的改动理由命中受保护属性（{cats}），已拒绝。"
            )

    return proposal


def check_against_rubric(proposal: Proposal, rubric: Rubric) -> None:
    """校验清单与当前标准对得上，避免"改一个不存在的维度"这种静默失效。"""
    index = _dimension_index(rubric)
    for patch in proposal.patches:
        if patch.op == "add":
            continue
        if (patch.kind, patch.target_id) not in index:
            existing = sorted(f"{k}:{i}" for k, i in index)
            raise RubricPatchError(
                f"变更 {patch.target_id}：当前标准里没有这个 {patch.kind} 维度。"
                f"现有维度：{'、'.join(existing) or '（空）'}"
            )


def _dimension_index(rubric: Rubric) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for kind, field in KIND_TO_FIELD.items():
        for dim in getattr(rubric, field):
            out.add((kind, dim.id))
    return out


def _dedupe_id(existing: set[str], want: str) -> str:
    """新维度 id 与现有冲突时加序号，而不是静默覆盖或直接报错。

    报错会让 HR 莫名其妙（"我只是加了条加分项"），
    静默覆盖会悄悄改掉一个已有维度——加序号是唯一不丢信息的选择。
    """
    if want not in existing:
        return want
    n = 2
    while f"{want}_{n}" in existing:
        n += 1
    return f"{want}_{n}"


def apply_patches(rubric: Rubric, proposal: Proposal, *, version: str | None = None) -> Rubric:
    """把变更清单应用到评分卡，返回**新的** Rubric（不改动传入对象）。

    最终结果通过 `Rubric.model_validate` 重建，因此：
    - 合规红线自动再拦一次（pydantic 校验器）
    - 维度 id 唯一性自动再校验
    - 字段类型/必填项由 schema 兜住
    """
    if proposal.refused:
        raise RubricPatchError(f"该要求已被拒绝，无法应用：{proposal.refused_reason}")

    check_against_rubric(proposal, rubric)

    data = rubric.model_dump(mode="json")
    for patch in proposal.patches:
        bucket: list[dict[str, Any]] = data[patch.field]
        if patch.op == "add":
            assert patch.after is not None
            new_dim = dict(patch.after)
            # 维度自身必带 kind 字段（hard_filter/bonus/red_flag），由所属类别决定
            new_dim.setdefault("kind", patch.kind)
            new_dim["id"] = _dedupe_id({d["id"] for d in bucket}, str(new_dim.get("id") or patch.target_id))
            bucket.append(new_dim)
        elif patch.op == "remove":
            data[patch.field] = [d for d in bucket if d["id"] != patch.target_id]
        else:  # modify
            assert patch.after is not None
            replaced = dict(patch.after)
            replaced["id"] = patch.target_id  # id 是结果对齐的锚点，不允许被改掉
            replaced.setdefault("kind", patch.kind)
            data[patch.field] = [replaced if d["id"] == patch.target_id else d for d in bucket]

    if version is not None:
        data["version"] = version

    try:
        return Rubric.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - 合规拦截与 schema 错误都归为"这次改动作废"
        raise RubricPatchError(f"应用变更后的标准不合法，已作废：{exc}") from exc


def describe_patches(proposal: Proposal) -> list[str]:
    """把清单变成给 HR 看的一行行说明（产品方案 §6.4：先说清"改了什么"）。"""
    op_text = {"add": "新增", "remove": "删除", "modify": "修改"}
    kind_text = {"hard_filter": "必须项", "bonus": "加分项", "red_flag": "减分项"}
    out: list[str] = []
    for patch in proposal.patches:
        head = f"{op_text[patch.op]}{kind_text[patch.kind]}「{_name_of(patch.before or patch.after)}」"
        detail = _detail_of(patch)
        out.append(f"{head}{detail}｜{patch.reason}" if detail else f"{head}｜{patch.reason}")
    return out


def _name_of(dim: dict[str, Any] | None) -> str:
    if not dim:
        return "?"
    return str(dim.get("name") or dim.get("id") or "?")


def _detail_of(patch: RubricPatch) -> str:
    if patch.op == "add":
        return ""
    before = patch.before or {}
    after = patch.after or {}
    if patch.op == "remove":
        return ""
    keys = ("name", "definition", "weight", "penalty", "rule")
    changed = [
        f"{k}：{before.get(k)!r} → {after.get(k)!r}"
        for k in keys
        if k in after and before.get(k) != after.get(k)
    ]
    return "（" + "；".join(changed) + "）" if changed else ""
