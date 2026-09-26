"""改动标准前的排名影响预估（技术方案 §7 第②段）。

**为什么这件事必须免费且即时**：HR 是拿这个预估来决定"要不要确认这次改动"的
（产品方案 §6.4）。如果预估要等十几秒、还要花 token，HR 就会跳过确认直接改，
整个"改标准必须确认后生效"的三道闸就形同虚设。

**能免费算的原因**：评分已经是纯函数，且结论里存了每个维度的判定与得分
（`DimensionResult.judgement` / `.score`）。换标准意味着：

- 改权重 / 扣分 → 得分可以按旧结论反推重算，**精确**；
- 删维度 → 该维度不再参与汇总，**精确**；
- 只改名称等元信息 → 判定不变，**精确**；
- **新增维度、或改动了判定口径（definition / 正反例 / rule / params）→ 判定本身可能变，
  无法从旧结论推出**。

最后这一类是这里最容易被糊过去的地方。做法是**明确标出"算不准"**
（`exact=False` + `unknown_candidates`），而不是给一个看起来很确定、
但重排后被推翻的假数字——预估错了比没有预估更伤信任。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import CandidateResult, DimensionResult, Judgement, ScreeningVerdict
from .rubric import Bonus, DimensionBase, RedFlag, Rubric
from .scoring import verdict_from_dimensions

# 判定口径相关字段：任何一个变了，旧判定就不再可信
_JUDGEMENT_KEYS = ("definition", "positive_examples", "negative_examples", "rule", "params")


@dataclass
class CandidateMove:
    """一份简历的分组/得分变化。`to_verdict is None` 表示无法预估。"""

    resume_id: str
    file_name: str
    from_verdict: ScreeningVerdict
    to_verdict: ScreeningVerdict | None
    from_score: float
    to_score: float | None
    note: str = ""


@dataclass
class ImpactEstimate:
    """改动对现有排名的影响。"""

    total: int = 0
    not_scored: int = 0
    exact: bool = True
    unknown_candidates: int = 0
    counts_before: dict[str, int] = field(default_factory=dict)
    counts_after: dict[str, int] = field(default_factory=dict)
    moves: list[CandidateMove] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.moves)

    def to_payload(self) -> dict:
        return {
            "total": self.total,
            "not_scored": self.not_scored,
            "exact": self.exact,
            "unknown_candidates": self.unknown_candidates,
            "changed": self.changed,
            "counts_before": self.counts_before,
            "counts_after": self.counts_after,
            "notes": self.notes,
            "moves": [
                {
                    "resume_id": m.resume_id,
                    "file_name": m.file_name,
                    "from_verdict": m.from_verdict.value,
                    "to_verdict": m.to_verdict.value if m.to_verdict else None,
                    "from_score": m.from_score,
                    "to_score": m.to_score,
                    "note": m.note,
                }
                for m in self.moves
            ],
        }


def _signature(dim: DimensionBase) -> tuple:
    """判定口径的指纹。变了就意味着旧判定不可复用。"""
    return tuple(_hashable(getattr(dim, k, None)) for k in _JUDGEMENT_KEYS)


def _hashable(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


def _cached_by_id(result: CandidateResult) -> dict[str, DimensionResult]:
    return {d.dimension_id: d for d in result.dimensions}


def _estimate_one(
    old: Rubric, new: Rubric, result: CandidateResult
) -> tuple[CandidateResult | None, bool, str]:
    """预估一份简历在 new 标准下的结论。

    返回 `(预估结果或 None, 是否精确, 说明)`。`None` 表示因为新增/改口径的维度
    而无法预估——不返回一个"看起来像结论"的对象，调用方就无法忽略这条分支。
    """
    cached = _cached_by_id(result)
    old_dims = {d.id: d for d in old.all_dimensions()}
    notes: list[str] = []
    exact = True

    dims: list[DimensionResult] = []

    for hf in new.hard_filters:
        hit = cached.get(hf.id)
        if hit is None:
            # 新增的必须项：这份简历从没被这一条判过，无法推测
            return None, False, f"新增必须项「{hf.name}」尚未对这份简历判定"
        if hf.id in old_dims and _signature(old_dims[hf.id]) != _signature(hf):
            return None, False, f"必须项「{hf.name}」的判定口径变了，旧判定不再适用"
        dims.append(hit.model_copy(update={"name": hf.name}))

    score = 0.0
    for dim in new.bonus:
        hit = cached.get(dim.id)
        if hit is None:
            return None, False, f"新增加分项「{dim.name}」尚未对这份简历判定"
        if dim.id in old_dims and _signature(old_dims[dim.id]) != _signature(dim):
            return None, False, f"加分项「{dim.name}」的判定口径变了，旧判定不再适用"
        old_weight = old_dims[dim.id].weight if dim.id in old_dims else 0.0
        if hit.judgement is not Judgement.SATISFIED:
            # 未满足/未提及/无法判定 → 命中度必为 0，得分贡献为 0，精确
            dims.append(hit.model_copy(update={"name": dim.name, "score": 0.0}))
            continue
        if old_weight <= 0:
            # 旧权重为 0 时得分恒为 0，命中度无法反推 → 只能算不准
            return None, False, f"加分项「{dim.name}」原权重为 0，命中度无法推算"
        level = hit.score / old_weight
        contribution = round(dim.weight * level, 2)
        score += contribution
        dims.append(hit.model_copy(update={"name": dim.name, "score": contribution}))

    for flag in new.red_flags:
        hit = cached.get(flag.id)
        if hit is None:
            return None, False, f"新增减分项「{flag.name}」尚未对这份简历判定"
        if flag.id in old_dims and _signature(old_dims[flag.id]) != _signature(flag):
            return None, False, f"减分项「{flag.name}」的判定口径变了，旧判定不再适用"
        if hit.judgement is Judgement.SATISFIED:
            score -= flag.penalty
            dims.append(hit.model_copy(update={"name": flag.name, "score": -flag.penalty}))
        else:
            dims.append(hit.model_copy(update={"name": flag.name, "score": 0.0}))

    if not exact:  # pragma: no cover - 当前三条返回 None 的分支已覆盖不精确的情形
        notes.append("存在无法精确预估的部分")

    verdict, _ = verdict_from_dimensions(dims)
    estimated = result.model_copy(
        update={
            "verdict": verdict,
            "score": round(max(0.0, min(score, new.max_possible_score())), 2),
            "max_score": new.max_possible_score(),
            "dimensions": dims,
        }
    )
    return estimated, True, "；".join(notes)


def estimate_impact(
    old: Rubric, new: Rubric, results: list[CandidateResult], *, not_scored: int = 0
) -> ImpactEstimate:
    """估算 new 标准下已有结论会怎么变。纯计算，不调用模型、不碰数据库。"""
    impact = ImpactEstimate(total=len(results) + not_scored, not_scored=not_scored)
    before: dict[str, int] = {}
    after: dict[str, int] = {}

    for result in results:
        before[result.verdict.value] = before.get(result.verdict.value, 0) + 1

        estimated, ok, note = _estimate_one(old, new, result)
        if not ok or estimated is None:
            impact.exact = False
            impact.unknown_candidates += 1
            # 无法预估的仍计入"改动前"的分布，但不臆造改动后的分布
            impact.moves.append(
                CandidateMove(
                    resume_id=result.resume_id,
                    file_name=result.file_name,
                    from_verdict=result.verdict,
                    to_verdict=None,
                    from_score=result.score,
                    to_score=None,
                    note=note,
                )
            )
            continue

        after[estimated.verdict.value] = after.get(estimated.verdict.value, 0) + 1
        if estimated.verdict is not result.verdict or abs(estimated.score - result.score) > 1e-9:
            impact.moves.append(
                CandidateMove(
                    resume_id=result.resume_id,
                    file_name=result.file_name,
                    from_verdict=result.verdict,
                    to_verdict=estimated.verdict,
                    from_score=result.score,
                    to_score=estimated.score,
                    note=note,
                )
            )

    impact.counts_before = before
    impact.counts_after = after

    if impact.unknown_candidates:
        impact.notes.append(
            f"有 {impact.unknown_candidates} 份简历涉及新增维度或口径变化，"
            "无法在改动前给出预估；确认后重排即可得到真实结果（只重跑评分，不重新解析）"
        )
    if impact.not_scored:
        # 只给数字不够：HR 看到"预计 3 人受影响"时，必须同时知道还有几份没算进来，
        # 否则会以为这次预估覆盖了全部简历（产品方案 §6.3 不静默跳过）。
        impact.notes.append(
            f"另有 {impact.not_scored} 份简历尚无结论（仍在处理中或需人工处理），"
            "未计入这次预估"
        )
    if impact.changed == 0 and impact.exact:
        impact.notes.append("按现有结论推算，这次改动不会改变任何人的分组")
    return impact
