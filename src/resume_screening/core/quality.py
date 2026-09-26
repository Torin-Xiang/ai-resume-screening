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

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ALNUM = re.compile(r"[0-9A-Za-z]")


def cjk_ratio_of(text: str) -> float:
    """中文字符占"中文+字母数字"的比例。用于识别乱码与扫描件。"""
    cjk = len(_CJK.findall(text))
    alnum = len(_ALNUM.findall(text))
    denom = cjk + alnum
    return (cjk / denom) if denom else 0.0


def assess_text_quality(text: str, *, pages: int = 0, has_images: bool = False) -> ParseQuality:
    """对一段文本做质量自检。

    `has_images` 只在 PDF 路径下有意义（用来区分"扫描件"与"真的没写内容"）；
    粘贴文本时传 False —— HR 亲手粘进来的内容不存在"图片型"这个问题。
    """
    stripped = (text or "").strip()
    char_count = len(stripped)
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
        cjk_ratio=round(cjk_ratio, 4),
        reasons=reasons,
        is_scanned=is_scanned,
    )
