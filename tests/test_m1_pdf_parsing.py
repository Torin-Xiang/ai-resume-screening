"""M1：PDF 解析 + 解析质量自检（方案 §2.1 ①、§9）。

验收标准：真实简历文本完整、无串栏；扫描件/图片型 PDF 被正确标记。
⚠️ 本文件用**合成的中文 PDF**（虚构人物）验证逻辑，合成的样本可以进 Git；
真实脱敏简历不入库，放本地 `fixtures/`（已 gitignore）。
真实样本已于 2026-09-26 提供 4 份，跑出并修掉一个判定漏洞（见本文件的
`test_watermarked_image_pdf_marked_needs_manual` 与 `test_avatar_pdf_is_not_mistaken_for_scanned`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resume_screening.adapters.parsers.pdf_pymupdf import PdfPymupdfParser, parse_pdf
from resume_screening.core.models import ParseStatus


def test_single_column_text_complete(single_column_pdf: Path) -> None:
    r = parse_pdf(single_column_pdf)
    assert r.quality.status is ParseStatus.OK, r.quality.reasons
    for kw in ("李示例", "示例大学", "计算机科学与技术", "算法工程师", "PyTorch"):
        assert kw in r.raw_text


def test_single_column_reading_order(single_column_pdf: Path) -> None:
    """单栏：按从上到下的阅读顺序，不出现乱序。"""
    r = parse_pdf(single_column_pdf)
    lines = [b["text"] for b in r.blocks]
    assert lines[0].startswith("个人简历")
    idx_edu = next(i for i, t in enumerate(lines) if "教育背景" in t)
    idx_work = next(i for i, t in enumerate(lines) if "工作经历" in t)
    assert idx_edu < idx_work  # 与源 PDF 的纵向顺序一致（教育背景在上方）


def test_two_column_no_column_mixing(two_column_pdf: Path) -> None:
    """双栏：先读完左栏再读右栏，禁止左右串栏。"""
    r = parse_pdf(two_column_pdf)
    text = r.raw_text
    left_first = text.index("工作描述一")
    left_last = text.index("工作描述三")
    right_first = text.index("项目描述一")
    right_last = text.index("项目描述三")
    assert left_first < left_last < right_first < right_last, (
        f"出现串栏：\n{text}"
    )


def test_two_column_blocks_marked(two_column_pdf: Path) -> None:
    r = parse_pdf(two_column_pdf)
    cols = {b["column"] for b in r.blocks}
    assert {0, 1, 2} & cols, f"应识别出通栏/左/右栏，实际列标记：{cols}"
    header = r.blocks[0]
    assert header["column"] == 0  # 顶部通栏标题


def test_two_column_content_not_lost(two_column_pdf: Path) -> None:
    r = parse_pdf(two_column_pdf)
    for kw in ("工作经历", "项目经历", "教育背景", "向量检索", "意图识别"):
        assert kw in r.raw_text


def test_scanned_pdf_marked_needs_manual(scanned_pdf: Path) -> None:
    """扫描件：必须标记 needs_manual 且不送入模型（方案 §9）。"""
    r = parse_pdf(scanned_pdf)
    assert r.quality.status is ParseStatus.NEEDS_MANUAL
    assert r.quality.is_scanned is True
    assert r.quality.usable is False
    assert any("扫描件" in s or "人工处理" in s for s in r.quality.reasons)


def test_empty_pdf_marked_needs_manual(empty_pdf: Path) -> None:
    r = parse_pdf(empty_pdf)
    assert r.quality.status is ParseStatus.NEEDS_MANUAL
    assert r.quality.usable is False


def test_watermarked_image_pdf_marked_needs_manual(watermarked_image_pdf: Path) -> None:
    """图片型 PDF + 水印文本层 → needs_manual（真实样本暴露的判定漏洞）。

    这是最危险的一类失败：水印把总字符数抬到阈值之上，于是"字符数太少"的扫描件
    判据整体失效，只给出 suspect —— 而 **suspect 仍会送入模型**。
    模型对着重复水印只能编造字段，直接违反"引不出原文就承认不知道"。
    """
    r = parse_pdf(watermarked_image_pdf)
    q = r.quality
    assert q.status is ParseStatus.NEEDS_MANUAL, q.reasons
    assert q.usable is False, "图片型 PDF 绝不能被送入模型"
    assert q.is_scanned is True
    assert q.image_coverage >= 0.7, f"整页图片的覆盖率应接近 1，实际 {q.image_coverage}"
    assert any("图片" in s and "人工处理" in s for s in q.reasons), q.reasons


def test_watermark_inflates_total_chars_but_not_unique_chars(
    watermarked_image_pdf: Path,
) -> None:
    """锁住故障成因：总字符数被水印"喂"到阈值之上，去重后才见真相。

    这个断言是防退化的关键——一旦哪天有人把判据改回"只看 char_count"，
    这条会立刻失败并说明为什么。
    """
    from resume_screening.core.quality import MIN_CHARS_OK

    q = parse_pdf(watermarked_image_pdf).quality
    assert q.char_count > MIN_CHARS_OK, (
        "夹具必须让总字符数超过阈值，否则它就复现不了真实故障"
    )
    assert q.unique_char_count < MIN_CHARS_OK, (
        f"去重后应所剩无几（实际 {q.unique_char_count}），这才是正文的真实体量"
    )


def test_avatar_pdf_is_not_mistaken_for_scanned(avatar_pdf: Path) -> None:
    """反向证明：含头像照片的正常简历不能被判成扫描件。

    头像也是图片。判据若写成"有没有图片"，这份正常简历会被直接踢去人工，
    等于把产品最想要的正常输入拒之门外。必须用**图片覆盖率**区分
    "整页是图"（≈1.0）与"角落有张头像"（≈0.02）。
    """
    r = parse_pdf(avatar_pdf)
    q = r.quality
    assert q.image_coverage < 0.7, f"头像不该占满版面，实际 {q.image_coverage}"
    assert q.status is ParseStatus.OK, q.reasons
    assert q.usable is True
    assert "李示例" in r.raw_text


def test_short_text_is_suspect(tmp_path: Path) -> None:
    """文本过短（解析不全）→ suspect，仍可用但需人工留意。"""
    from tests.conftest import write_pdf

    p = write_pdf(tmp_path / "short.pdf", [(50, 60, "姓名：李示例"), (50, 90, "学历：本科")])
    r = parse_pdf(p)
    assert r.quality.status is ParseStatus.SUSPECT
    assert r.quality.usable is True


def test_short_text_with_full_page_image_is_needs_manual(tmp_path: Path) -> None:
    """文本短 + 整页图片 → needs_manual（走的是"图片覆盖率"这条新判据）。

    刻意取 repeats=3（约 143 字）：既超过"极少文本"的 50 字下限、又低于 200 字，
    这样才会落到新判据上；若取 1 次，走的是原有的 50 字判据，等于没验到新分支。
    """
    from tests.conftest import write_watermarked_image_pdf

    p = write_watermarked_image_pdf(tmp_path / "few.pdf", repeats=3)
    q = parse_pdf(p).quality
    assert 50 < q.char_count < 200, f"夹具取材没落在新判据区间，实际 {q.char_count}"
    assert q.status is ParseStatus.NEEDS_MANUAL, q.reasons
    assert q.usable is False


def test_garbled_text_is_suspect(tmp_path: Path) -> None:
    """中文占比异常 → suspect（乱码识别）。"""
    from tests.conftest import write_pdf

    body = " ".join(["abcd efgh ijkl mnop qrst uvwx"] * 20)
    p = write_pdf(tmp_path / "garbled.pdf", [(50, 60 + i * 14, body) for i in range(20)])
    r = parse_pdf(p)
    assert r.quality.status is ParseStatus.SUSPECT
    assert any("中文字符占比" in s for s in r.quality.reasons)


def test_parser_is_pure_function(single_column_pdf: Path) -> None:
    """纯函数：同一输入两次解析结果一致，且不依赖调用顺序/全局状态（方案 §9）。"""
    a = parse_pdf(single_column_pdf)
    b = parse_pdf(single_column_pdf)
    assert a.raw_text == b.raw_text
    assert a.resume_id == b.resume_id
    assert a.quality == b.quality


def test_resume_id_and_source_path(single_column_pdf: Path) -> None:
    r = parse_pdf(single_column_pdf)
    assert len(r.resume_id) == 16
    assert r.file_name == "single.pdf"
    assert Path(r.source_path).is_absolute()


def test_parser_protocol_conformance() -> None:
    from resume_screening.core.ports import ResumeParser

    p = PdfPymupdfParser()
    assert isinstance(p, ResumeParser)
    assert ".pdf" in p.suffixes


def test_unsupported_extension_raises(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("not a pdf", encoding="utf-8")
    with pytest.raises(Exception):
        parse_pdf(p)
