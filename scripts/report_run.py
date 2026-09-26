"""汇总一次 screen 的结果：排名、9 项字段抽取、token 统计、M2 字段准确率。

    .venv/Scripts/python.exe scripts/report_run.py --run data/runs/<id>/run.json

`--truth` 给一份**人工核对基准**（JSON，键为简历文件名）时，额外输出 M2 的
字段准确率。基准里只写从原文确认过的值；拿不准的字段写 `null`，脚本会把它当成
"原文确实没有这一项"，而不是当成你漏填。

为什么要有这个脚本：`run.json` 只存**结论**（分数、判定、证据），
**不存 9 项提取字段**——那几个字段在 `data/cache/` 的 `ResumeDocument` 里。
出报告必须同时读这两处，写一次比每次手敲可靠。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

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
FIELD_LABELS = {
    "name": "姓名",
    "contact": "联系方式",
    "education": "学历",
    "school": "院校",
    "major": "专业",
    "years_of_experience": "工作年限",
    "current_company": "现公司",
    "current_title": "现岗位",
    "skills": "技能",
    "location": "现居城市",
}
VERDICT_LABELS = {"pass": "推荐", "needs_review": "待确认", "reject": "不推荐"}
STATUS_LABELS = {
    "extracted": "已提取",
    "not_mentioned": "未提及",
    "unparsable": "无法结构化",
}

_PUNCT = re.compile(r"[\s|（）()、，,；;：:·・\-—–_/]")


def _norm(value: Any) -> str:
    return _PUNCT.sub("", str(value or "")).lower()


def _norm_contact(value: Any) -> str:
    """联系方式按数字比：`199-6217-1738` 与 `19962171738` 是同一个号。"""
    return re.sub(r"\D", "", str(value or ""))


def load_docs(cache_dir: Path) -> dict[str, dict[str, Any]]:
    """缓存里的 `ResumeDocument`，按 resume_id 索引。"""
    docs: dict[str, dict[str, Any]] = {}
    for path in sorted(cache_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("resume_id"):
            docs[doc["resume_id"]] = doc
    return docs


def one_line_reason(result: dict[str, Any]) -> str:
    """给排名表用的"一句话理由"，优先说硬门槛。"""
    dims = result.get("dimensions") or []
    for dim in dims:
        if dim.get("kind") == "hard_filter" and dim.get("judgement") != "satisfied":
            return f"[{dim['name']}] {dim['reason']}"
    scored = [d for d in dims if d.get("kind") == "bonus" and d.get("score")]
    if scored:
        top = max(scored, key=lambda d: d["score"])
        return f"[{top['name']}] {top['reason']}"
    return result.get("notes") and "；".join(result["notes"]) or "（无附加说明）"


def print_overview(run: dict[str, Any]) -> None:
    print("=" * 72)
    print("① 运行概览")
    print("=" * 72)
    for label, key in (
        ("run_id", "run_id"),
        ("岗位", "job_title"),
        ("评分卡版本", "rubric_version"),
        ("评分卡文件", "rubric_path"),
        ("简历目录", "input_dir"),
        ("跑完时间", "created_at"),
    ):
        print(f"  {label:<12}{run.get(key, '')}")
    print(f"  {'简历份数':<12}{len(run.get('results') or [])}")


def print_ranking(run: dict[str, Any]) -> None:
    print()
    print("=" * 72)
    print("② 优先级排名（已按 name/分数 排序，与 Excel 一致）")
    print("=" * 72)
    print(f"  {'#':<3}{'姓名':<8}{'结论':<8}{'得分':<12}{'必须项'}")
    for i, r in enumerate(run.get("results") or [], 1):
        verdict = VERDICT_LABELS.get(r.get("verdict", ""), r.get("verdict", ""))
        score = f"{r.get('score', 0):g}/{r.get('max_score', 0):g}"
        hard = [d for d in (r.get("dimensions") or []) if d.get("kind") == "hard_filter"]
        hard_txt = "、".join(
            f"{d['name'][:8]}={d['judgement']}" for d in hard
        ) or "—"
        print(f"  {i:<3}{(r.get('candidate_name') or '（无）'):<8}{verdict:<8}{score:<12}{hard_txt}")
    print()
    for i, r in enumerate(run.get("results") or [], 1):
        print(f"  {i}. {r.get('candidate_name') or '（无）'}：{one_line_reason(r)}")


def print_fields(run: dict[str, Any], docs: dict[str, dict[str, Any]]) -> None:
    print()
    print("=" * 72)
    print("③ 9 项字段抽取结果（含原文证据）")
    print("=" * 72)
    for r in run.get("results") or []:
        doc = docs.get(r.get("resume_id") or "")
        print()
        print(f"  ▸ {r.get('file_name')}")
        if doc is None:
            print("    （缓存中找不到该简历的提取结果，无法展示字段）")
            continue
        fields = doc.get("fields") or {}
        print(f"    {'字段':<10}{'状态':<10}{'值':<26}原文证据")
        for name in CORE_FIELDS + ("location",):
            item = fields.get(name) or {}
            status = STATUS_LABELS.get(item.get("status", ""), item.get("status", ""))
            value = item.get("value")
            if isinstance(value, list):
                value = "、".join(str(v) for v in value)
            evidence = item.get("evidence") or {}
            quote = (evidence.get("quote") or "") if isinstance(evidence, dict) else ""
            print(
                f"    {FIELD_LABELS.get(name, name):<10}{status:<10}{str(value or '—'):<26}"
                f"{quote[:40] or '（无）'}"
            )


def print_tokens(run: dict[str, Any]) -> None:
    print()
    print("=" * 72)
    print("④ token 消耗统计")
    print("=" * 72)
    print(f"  模型调用次数    {run.get('llm_call_count', 0)}")
    print(f"  累计 token      {run.get('llm_cost_tokens', 0)}")
    print("  说明：run.json 由 RunRecord 保存，只记录**总次数与总 token**；")
    print("        prompt / completion 的拆分见 `MeteredLLM.snapshot()`（CLI 打印在标准输出）。")


def score_truth(run: dict[str, Any], docs: dict[str, dict[str, Any]], truth: dict[str, Any]) -> None:
    """M2：逐字段比对"提取值"与"人工核对基准"。"""
    print()
    print("=" * 72)
    print("⑤ M2 字段准确率（人工核对基准比对）")
    print("=" * 72)

    hits = misses = skipped = 0
    per_field: dict[str, list[int]] = {f: [0, 0] for f in CORE_FIELDS}
    rows: list[str] = []

    for r in run.get("results") or []:
        file_name = r.get("file_name") or ""
        expected = truth.get(file_name)
        if expected is None:
            rows.append(f"  {file_name}：基准里没有这份，跳过")
            skipped += 1
            continue
        doc = docs.get(r.get("resume_id") or "") or {}
        fields = doc.get("fields") or {}
        for name in CORE_FIELDS:
            want = expected.get(name)
            item = fields.get(name) or {}
            got = item.get("value")
            status = item.get("status")

            if name == "contact":
                ok = _norm_contact(want) == _norm_contact(got)
                same_absence = not _norm_contact(want) and not _norm_contact(got)
            else:
                ok = _norm(want) == _norm(got)
                same_absence = not _norm(want) and not _norm(got)

            if want is None and status == "not_mentioned":
                ok, note = True, "原文确无此项，双方一致"
            elif want is None and got:
                ok, note = False, f"原文未见，却抽出「{got}」"
            elif want is not None and status != "extracted":
                ok, note = False, f"漏抽（状态 {status}）"
            elif ok:
                note = "一致"
            else:
                note = f"值不符：期望「{want}」实得「{got or '—'}」"
            if name == "contact" and want is not None and status == "extracted":
                ok = _norm_contact(want) == _norm_contact(got)
                note = "号码一致" if ok else note

            hits += int(ok)
            misses += int(not ok)
            per_field[name][0] += int(ok)
            per_field[name][1] += 1
            if not ok:
                rows.append(
                    f"  ✗ {file_name.split('】')[-1][:10]:<12}{FIELD_LABELS.get(name, name):<10}{note}"
                )

    total = hits + misses
    print()
    if not total:
        print("  基准为空或与本次运行不匹配，未统计。")
        return
    for name in CORE_FIELDS:
        ok, n = per_field[name]
        if n:
            print(f"  {FIELD_LABELS.get(name, name):<10}{ok}/{n}  {ok / n:.0%}")
    print(f"\n  合计 {hits}/{total} = **{hits / total:.1%}**（M2 验收标准 ≥ 90%）")
    if rows:
        print("\n  不通过的明细：")
        for line in rows:
            print(line)


def main() -> int:
    ap = argparse.ArgumentParser(description="汇总一次 screen 的结果")
    ap.add_argument("--run", required=True, help="run.json 路径")
    ap.add_argument("--cache-dir", default="data/cache", help="提取缓存目录（默认 data/cache）")
    ap.add_argument("--truth", help="人工核对基准 JSON（可选）")
    args = ap.parse_args()

    run_path = Path(args.run)
    if not run_path.is_file():
        print(f"找不到 run.json：{run_path}")
        return 1
    run = json.loads(run_path.read_text(encoding="utf-8"))
    docs = load_docs(Path(args.cache_dir))

    print_overview(run)
    print_ranking(run)
    print_fields(run, docs)
    print_tokens(run)

    if args.truth:
        truth_path = Path(args.truth)
        truth = json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.is_file() else {}
        score_truth(run, docs, truth)
    else:
        print()
        print("  （加了 --truth 才会输出 M2 字段准确率）")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
