"""PDF 解析器适配器（PyMuPDF）——方案 §2.1 ①、§9 最大风险点。

硬性约束：
1. **纯函数**：文件路径 → ParseResult，不掺评分逻辑、不持有全局状态。
   真实 PDF 版式千变万化，解析器会不断加特例；只有保持纯函数才不会在加特例时失控。
2. 按坐标块重排：先分栏（左栏 → 右栏），再按行排序，避免分栏简历串栏。
3. 解析质量自检：扫描件 / 图片型 PDF（含只有水印的文本层）→ 标记 needs_manual，
   **不送入模型**。判据与阈值统一放在 core/quality.py，与"粘贴文本"兜底共用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:  # PyMuPDF 新版本推荐 pymupdf 顶层模块，旧版本为 fitz
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]

from resume_screening.core.ids import resume_id_of
from resume_screening.core.models import ParseResult
from resume_screening.core.quality import assess_text_quality

# —— 版式重排参数 ——
# 质量阈值（字符数 / 中文占比）见 core/quality.py：与"粘贴文本"兜底共用一份，
# 避免同一份内容在 PDF 路径与粘贴路径下得到不同结论。
LINE_TOLERANCE_FACTOR = 0.6  # 行聚类容差 = 该系数 × 行高中位数
FULL_WIDTH_RATIO = 0.6      # 块宽超过页宽该比例 → 视为通栏块（标题/分隔）
GAP_BIN = 5.0               # 列投影桶宽（pt）
GAP_MIN_BINS = 3            # 连续空白桶数达到该值才算"列间距"


class UnsupportedFileError(ValueError):
    """文件类型不是该解析器支持的格式（应由上层换解析器，而不是硬解）。"""


@dataclass
class _Block:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    page: int
    column: int = 0  # 0=通栏, 1=左, 2=右


class PdfPymupdfParser:
    """PDF → 结构化文本块 + 重排后纯文本。"""

    name = "pdf-pymupdf"
    suffixes = (".pdf",)

    def parse(self, path: str | Path) -> ParseResult:
        p = Path(path)
        if p.suffix.lower() not in self.suffixes:
            raise UnsupportedFileError(
                f"{self.name} 只支持 {self.suffixes}，收到：{p.name}（应换用对应解析器适配器）"
            )
        if not p.exists():
            raise FileNotFoundError(f"简历文件不存在：{p}")
        blocks: list[_Block] = []
        pages = 0
        has_images = False
        image_coverage = 0.0

        with fitz.open(p) as doc:
            pages = doc.page_count
            for pno in range(pages):
                page = doc[pno]
                for b in page.get_text("blocks"):
                    x0, y0, x1, y1, text, _no, btype = b[:7]
                    if btype != 0:  # 1=图片块
                        if btype == 1:
                            has_images = True
                        continue
                    t = (text or "").strip()
                    if not t:
                        continue
                    blocks.append(_Block(x0, y0, x1, y1, t, pno + 1))
                if not has_images and page.get_images(full=False):
                    has_images = True
                image_coverage = max(image_coverage, self._image_coverage(page))

        ordered = self._reorder(blocks)
        raw_text = "\n".join(b.text for b in ordered)
        quality = assess_text_quality(
            raw_text, pages=pages, has_images=has_images, image_coverage=image_coverage
        )

        return ParseResult(
            resume_id=resume_id_of(p),
            source_path=str(p.resolve()),
            file_name=p.name,
            raw_text=raw_text,
            quality=quality,
            pages=pages,
            blocks=[
                {
                    "page": b.page,
                    "x0": round(b.x0, 1),
                    "y0": round(b.y0, 1),
                    "x1": round(b.x1, 1),
                    "y1": round(b.y1, 1),
                    "column": b.column,
                    "text": b.text,
                }
                for b in ordered
            ],
        )

    # ---------------- 版式重排 ----------------
    def _reorder(self, blocks: list[_Block]) -> list[_Block]:
        if not blocks:
            return []
        width = max(b.x1 for b in blocks)
        full = [b for b in blocks if (b.x1 - b.x0) > FULL_WIDTH_RATIO * width]
        full_ids = {id(b) for b in full}
        cols = [b for b in blocks if id(b) not in full_ids]
        for b in full:
            b.column = 0

        split_x = self._detect_column_split(cols, width)
        if split_x is None:
            for b in cols:
                b.column = 1
            return self._sort_rows(full + cols)

        left = [b for b in cols if (b.x0 + b.x1) / 2 < split_x]
        right = [b for b in cols if b not in left]
        for b in left:
            b.column = 1
        for b in right:
            b.column = 2

        # 通栏块（标题/分节）作为锚点：锚点 → 左栏该段 → 右栏该段 → 下一锚点
        anchors = sorted(full, key=lambda b: (b.page, b.y0))
        out: list[_Block] = []
        cursor: tuple[int, float] = (-1, -1.0)  # (page, y0)
        for a in anchors + [_Block(0, float("inf"), 0, 0, "", 10**6)]:
            seg_left = [b for b in left if cursor <= (b.page, b.y0) < (a.page, a.y0)]
            seg_right = [b for b in right if cursor <= (b.page, b.y0) < (a.page, a.y0)]
            if a.page != 10**6:
                out.append(a)
            out.extend(self._sort_rows(seg_left))
            out.extend(self._sort_rows(seg_right))
            cursor = (a.page, a.y0)
        return out

    @staticmethod
    def _detect_column_split(blocks: list[_Block], width: float) -> float | None:
        """列投影找垂直空白带：存在且两侧都有文本 → 判为分栏。"""
        if len(blocks) < 4 or width <= 0:
            return None
        nbins = max(int(width / GAP_BIN) + 1, 1)
        occupied = [0] * nbins
        for b in blocks:
            lo = max(int(b.x0 / GAP_BIN), 0)
            hi = min(int(b.x1 / GAP_BIN), nbins - 1)
            for i in range(lo, hi + 1):
                occupied[i] = 1

        lo_limit, hi_limit = int(0.15 * nbins), int(0.85 * nbins)
        best: tuple[int, int] | None = None  # (length, start)
        start: int | None = None
        for i in range(lo_limit, hi_limit):
            if occupied[i] == 0:
                start = i if start is None else start
            else:
                if start is not None and i - start >= GAP_MIN_BINS:
                    cand = (i - start, start)
                    if best is None or cand[0] > best[0]:
                        best = cand
                start = None
        if start is not None and hi_limit - start >= GAP_MIN_BINS:
            cand = (hi_limit - start, start)
            if best is None or cand[0] > best[0]:
                best = cand
        if best is None:
            return None
        split_x = (best[1] + best[0] / 2) * GAP_BIN
        left_n = sum(1 for b in blocks if (b.x0 + b.x1) / 2 < split_x)
        right_n = len(blocks) - left_n
        if left_n < 2 or right_n < 2:  # 一侧几乎没有内容 → 不是真分栏
            return None
        return split_x

    @staticmethod
    def _sort_rows(blocks: list[_Block]) -> list[_Block]:
        """按 (页 → 行带 → x) 排序：行带用行高中位数做容差，避免基线抖动导致乱序。"""
        if len(blocks) <= 1:
            return sorted(blocks, key=lambda b: (b.page, b.y0, b.x0))
        heights = sorted(b.y1 - b.y0 for b in blocks)
        median_h = heights[len(heights) // 2] or 10.0
        tol = max(median_h * LINE_TOLERANCE_FACTOR, 2.0)
        return sorted(blocks, key=lambda b: (b.page, round(b.y0 / tol), b.x0))

    # ---------------- 质量自检 ----------------
    @staticmethod
    def _image_coverage(page: object) -> float:
        """单页图片面积之和 / 版面面积，上限 1.0。

        **为什么不能用 `get_text("blocks")` 的图片块**（实测教训）：
        真实样本上 `get_text("blocks")` 一个图片块都不返回（`btype` 全是 0），
        所以上面那个 `btype == 1` 分支在这些文件上是走不到的——
        `has_images` 实际全靠 `page.get_images()` 兜住。
        面积还得从 `get_image_info()` 拿 bbox。

        用面积而不是"有没有图片"：**头像照片也是图片**，
        只判存在性会把正常简历误判成扫描件（见 core/quality.py 的说明）。
        """
        rect = getattr(page, "rect", None)
        area = (rect.width * rect.height) if rect is not None else 0.0
        if area <= 0:
            return 0.0
        covered = 0.0
        try:
            infos = page.get_image_info()  # type: ignore[attr-defined]
        except AttributeError:  # 老版本 PyMuPDF 无此接口 → 退化为"有图即满覆盖"，交由文本层判据兜底
            return 1.0 if page.get_images(full=False) else 0.0  # type: ignore[attr-defined]
        for info in infos:
            r = fitz.Rect(info["bbox"])
            covered += max(r.width, 0.0) * max(r.height, 0.0)
        return min(covered / area, 1.0)

    # 阈值与判定逻辑在 core/quality.py：粘贴文本兜底要复用同一套，
    # 放在 core 才能既被适配器用、又不让适配器之间互相依赖。


def parse_pdf(path: str | Path) -> ParseResult:
    """便捷函数：保持"纯函数：文件路径 → ParseResult"的调用形态。"""
    return PdfPymupdfParser().parse(path)
