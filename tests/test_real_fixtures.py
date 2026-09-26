"""真实脱敏简历的回归测试（本地 `fixtures/`，不入库）。

为什么单独一个文件：合成 PDF 验的是"我想到的版式"，真实样本验的是"实际会遇到的版式"。
2026-09-26 拿到 4 份真实样本后跑出的第一个问题（图片型 PDF 被误判为 suspect 而送入模型）
就是合成样本完全没覆盖到的。

两条设计约束：
1. **文件名绝不写进代码** —— 测试文件会入库，写入真实候选人姓名等于把个人信息又搬回
   仓库，正是方案 §9 要禁止的事。所以用 `参数 id = sample-N` 做匿名化。
2. **无样本时自动跳过** —— 干净检出（CI 或他人拉取）下 `fixtures/` 是空的，
   不能让测试因此变红；本地放了样本就自动生效。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from resume_screening.adapters.parsers.pdf_pymupdf import parse_pdf
from resume_screening.core.quality import IMAGE_COVERAGE_SCANNED, MIN_CHARS_SCANNED

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_LOCAL_SAMPLES = sorted(FIXTURES.glob("*.pdf"))
# 匿名化的参数 id：真实姓名不进仓库
_IDS = [f"sample-{i + 1}" for i in range(len(_LOCAL_SAMPLES))]

pytestmark = pytest.mark.skipif(
    not _LOCAL_SAMPLES,
    reason="本地 fixtures/ 无真实样本（不入库）；放入脱敏 PDF 后自动生效",
)


@pytest.mark.parametrize("path", _LOCAL_SAMPLES, ids=_IDS)
def test_real_resume_always_gets_a_decidable_verdict(path: Path) -> None:
    """每份真实简历都必须给出明确结论：可送模型 或 明确需人工。

    禁止的中间态是"文本其实不可用，却没被标记"——那会让模型拿着水印去编字段。
    """
    q = parse_pdf(path).quality
    if q.usable:
        # 可送模型 → 正文不能只剩几十字；若仍有可疑之处必须写明理由
        assert q.unique_char_count >= MIN_CHARS_SCANNED or q.reasons, (
            f"文本去重后仅 {q.unique_char_count} 字却既可用又无任何说明"
        )
    else:
        assert q.reasons, "判为不可用却不给理由，HR 无从知道该做什么"


@pytest.mark.parametrize("path", _LOCAL_SAMPLES, ids=_IDS)
def test_real_full_page_image_is_never_sent_to_model(path: Path) -> None:
    """整页图片（含只有水印的文本层）→ 绝不送入模型。

    这是真实样本暴露的那条漏洞的回归守卫，也是本文件存在的主要理由。
    """
    q = parse_pdf(path).quality
    if q.image_coverage >= IMAGE_COVERAGE_SCANNED:
        assert q.usable is False, (
            f"图片覆盖 {q.image_coverage:.2f} 的页面被判为可用："
            f"文本层只有 {q.unique_char_count} 字，送模型只会得到编造的字段"
        )
        assert any("人工" in s for s in q.reasons), q.reasons


def test_local_samples_are_present_but_not_tracked() -> None:
    """样本必须在本地存在，但**绝不能**被 Git 跟踪（方案 §9）。

    把"我忘了 gitignore"变成会失败的测试，而不是等到某次提交才发现。
    """
    assert _LOCAL_SAMPLES, "本地样本应存在（否则本文件会整体跳过，等于没验）"
    tracked = subprocess.run(
        ["git", "ls-files", "fixtures"],
        cwd=FIXTURES.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not tracked.stdout.strip(), f"这些样本进了 Git，必须移除：{tracked.stdout}"
