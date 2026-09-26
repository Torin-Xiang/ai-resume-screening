"""解析质量自检（产品方案 §6.1 / 技术方案 §4）。

抽成独立模块的原因：**粘贴文本兜底**与 PDF 解析必须用同一套阈值。
两边各写一份的话，"什么样的内容算可用"就会有两个定义，然后慢慢漂移——
最终表现为"同一份内容，PDF 上传说不行、粘文本说行"这种无法向 HR 解释的差异。

阈值集中放置，便于按真实样本调整（技术方案 §9 风险：中文版式千变万化）。
"""

from __future__ import annotations

import re

from .models import ParseQuality, ParseStatus

MIN_CHARS_OK = 200  # 少于该字符数 → suspect（中文简历通常远大于此）
MIN_CHARS_SCANNED = 50  # 少于该字符数且有图片 → 判定为扫描件
MIN_CJK_RATIO = 0.03  # 中文占比过低 → suspect（乱码/纯外文简历，需人工留意）
# 单页图片面积占版面比例达到该值 → 该页主体是图片（而不是"含一张头像照片"）
IMAGE_COVERAGE_SCANNED = 0.7

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ALNUM = re.compile(r"[0-9A-Za-z]")


def cjk_ratio_of(text: str) -> float:
    """中文字符占"中文+字母数字"的比例。用于识别乱码与扫描件。"""
    cjk = len(_CJK.findall(text))
    alnum = len(_ALNUM.findall(text))
    denom = cjk + alnum
    return (cjk / denom) if denom else 0.0


def unique_char_count_of(text: str) -> int:
    """去掉完全重复的行之后剩下的字符数。

    为什么不能只看总字符数：**重复的内容不携带信息**。
    招聘平台会在图片型简历上铺一层水印（同一串字符重复几十次），
    总字符数因此被"喂"到阈值之上，于是"图片型 PDF"的判据（字符数太少）整体失效。
    真实样本上见过：正文一个字都没提到，却有 746 字全是同一条水印。
    """
    lines = {ln.strip() for ln in (text or "").splitlines() if ln.strip()}
    return sum(len(ln) for ln in lines)


def assess_text_quality(
    text: str,
    *,
    pages: int = 0,
    has_images: bool = False,
    image_coverage: float = 0.0,
) -> ParseQuality:
    """对一段文本做质量自检。

    `has_images` / `image_coverage` 只在 PDF 路径下有意义（用来区分"扫描件"与"真的没写内容"）；
    粘贴文本时留默认值 —— HR 亲手粘进来的内容不存在"图片型"这个问题。

    `image_coverage` 取"各页中最高的一页图片覆盖率"。
    用覆盖率而不是"有没有图片"，是因为**头像照片也是图片**：
    只判存在性会把正常简历误判成扫描件，覆盖率能把"整页是图"和"角落有张头像"分开
    （真实样本实测：整页图片型 1.00，含头像的正常简历 0.00–0.04）。
    """
    stripped = (text or "").strip()
    char_count = len(stripped)
    unique_chars = unique_char_count_of(stripped)
    cjk_ratio = cjk_ratio_of(stripped)

    reasons: list[str] = []
    status = ParseStatus.OK
    is_scanned = False

    if char_count == 0:
        status = ParseStatus.NEEDS_MANUAL
        is_scanned = has_images or pages > 0
        reasons.append("未提取到任何文本：疑似扫描件或图片型 PDF，需人工处理")
    elif char_count < MIN_CHARS_SCANNED and has_images:
        status = ParseStatus.NEEDS_MANUAL
        is_scanned = True
        reasons.append(
            f"文本仅 {char_count} 字且含图片：疑似扫描件，需人工处理（不送入模型）"
        )
    elif image_coverage >= IMAGE_COVERAGE_SCANNED and unique_chars < MIN_CHARS_OK:
        # 页面主体是图片，而文本层去掉重复后所剩无几 → 提到的是水印，不是正文。
        # 必须判 NEEDS_MANUAL 而不是 SUSPECT：SUSPECT 仍会送入模型，
        # 模型对着水印只能编造字段，这违反"引不出原文就承认不知道"。
        status = ParseStatus.NEEDS_MANUAL
        is_scanned = True
        reasons.append(
            f"页面主体为图片（占版面 {image_coverage:.0%}），文本层去除重复后仅 {unique_chars} 字"
            f"（总 {char_count} 字，多为水印重复）：正文未提取到，疑似扫描件/图片型 PDF，"
            "需人工处理（不送入模型）"
        )
    elif char_count < MIN_CHARS_OK:
        status = ParseStatus.SUSPECT
        reasons.append(
            f"文本仅 {char_count} 字，少于阈值 {MIN_CHARS_OK}：可能解析不全，请人工留意"
        )

    if char_count > 0 and cjk_ratio < MIN_CJK_RATIO:
        if status is ParseStatus.OK:
            status = ParseStatus.SUSPECT
        reasons.append(
            f"中文字符占比 {cjk_ratio:.1%}，低于 {MIN_CJK_RATIO:.0%}："
            "可能为乱码、编码异常或纯外文简历，请人工留意"
        )

    return ParseQuality(
        status=status,
        page_count=pages,
        char_count=char_count,
        unique_char_count=unique_chars,
        image_coverage=round(image_coverage, 4),
        cjk_ratio=round(cjk_ratio, 4),
        reasons=reasons,
        is_scanned=is_scanned,
    )
