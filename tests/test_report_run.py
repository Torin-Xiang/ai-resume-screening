"""`scripts/report_run.py`：复盘报告脚本里最容易出错的两处——值比对与显示。

它不在包里（`scripts/` 不是 package），只能按文件路径加载。

单独测它的理由：**比对口径写错不会报任何错，只会稳稳地输出一个错的准确率**，
而那个数字看起来就像"模型抽错了"。已经踩过一次：

- `skills` 的核心类型是 `list[str]`，人工基准写的是一行字符串。
  `str(["Dify", "Codex"])` 得到 `['dify', 'codex']`——方括号和引号都不在清洗表里，
  于是**抽对了也永远判为"值不符"**，把评测工具自己的 bug 伪装成模型缺陷。
- 排名表的"必须项"列曾用「、」连接，而维度名自己就带顿号
  （「有 AI 产品、AIGC、大模型应用」），两件事被粘成一件。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "report_run.py"


@pytest.fixture(scope="module")
def report() -> Any:
    spec = importlib.util.spec_from_file_location("report_run_under_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None, _MODULE_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ——————————————————— 值比对口径 ———————————————————


def test_list_value_is_flattened_before_comparison(report: Any) -> None:
    """列表字段与"顿号连接的一行字符串"必须判为一致。"""
    assert report._norm(["Dify", "Codex", "Python"]) == report._norm("Dify、Codex、Python")


def test_empty_values_all_normalise_to_empty(report: Any) -> None:
    for value in (None, "", [], ()):
        assert report._norm(value) == ""


def test_separator_and_case_differences_are_not_mismatches(report: Any) -> None:
    """空格与标点写法不同不是抽错：`PRD撰写` 与 `PRD 撰写` 是一回事。"""
    assert report._norm("PRD 撰写") == report._norm("PRD撰写")
    assert report._norm("AI/LLM") == report._norm("ai llm")


def test_contact_comparison_ignores_formatting(report: Any) -> None:
    assert report._norm_contact("138-0013-8000") == report._norm_contact("13800138000")


# ——————————————————— 显示 ———————————————————


def test_clip_leaves_short_text_alone(report: Any) -> None:
    assert report._clip("学历门槛", 12) == "学历门槛"
    assert report._clip(None, 12) == ""
    assert report._clip("", 12) == ""


def test_clip_marks_truncation_instead_of_cutting_silently(report: Any) -> None:
    """静默切断看着像数据损坏；留 `…` 才读得出"这里被截过"。"""
    clipped = report._clip("一个非常长的维度名字需要被截断", 8)

    assert clipped.endswith("…")
    assert len(clipped) == 8


def test_display_flattens_lists_and_marks_empty_values(report: Any) -> None:
    """展示层要能把 `list[str]` 拼成一行；空值是 `—` 而不是 `None`。"""
    assert report._display(["Dify", "Codex"]) == "Dify、Codex"
    assert report._display(None) == "—"
    assert report._display("") == "—"
    assert report._display([]) == "—"


def test_value_mismatch_output_carries_a_caveat(
    report: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """「值不符」必须带上"不等于抽错"的提示。

    人工基准是关键词清单，抽取结果常更全或换同义词。没有这句提示，
    读者会只盯着百分比，把"抽得更全"误判成"功能坏了"。
    """
    run = {"results": [{"resume_id": "r1", "file_name": "a.pdf"}]}
    docs = {"r1": {"fields": {"school": {"value": "清华大学（985）", "status": "extracted"}}}}
    truth = {"a.pdf": {"school": "清华大学"}}

    report.score_truth(run, docs, truth)
    out = capsys.readouterr().out

    assert "值不符" in out
    assert "不等于抽错" in out


def test_hard_filter_column_does_not_glue_names_and_judgements_together(
    report: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """维度名自带顿号，所以列内分隔符必须换一个，否则两件事会被粘成一件。"""
    run = {
        "results": [
            {
                "candidate_name": "张三",
                "verdict": "pass",
                "score": 10,
                "max_score": 10,
                "dimensions": [
                    {
                        "kind": "hard_filter",
                        "name": "有 AI 产品、AIGC 应用",
                        "judgement": "satisfied",
                    },
                    {"kind": "hard_filter", "name": "学历门槛", "judgement": "satisfied"},
                ],
                "notes": [],
            }
        ]
    }

    report.print_ranking(run)
    out = capsys.readouterr().out

    assert out.count("=satisfied") == 2, out
    # 关键：两项之间不能是顿号——维度名自身就带顿号，用顿号连会把两件事粘成一件
    assert "、satisfied" not in out, out
    assert "；" in out, out
