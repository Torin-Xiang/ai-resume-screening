"""测试夹具。

⚠️ 说明（方案 §9 风险：个人信息泄露）：
真实脱敏简历样本**不得进 Git**。本文件只生成**合成的测试用 PDF**（虚构人物），
用于验证解析器的版式重排与质量自检逻辑；真实样本由使用者放在本地 `fixtures/` 目录
（已 gitignore），不参与仓库提交。
"""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]

import pytest

def _white_png(width: int = 400, height: int = 500) -> bytes:
    """生成一张纯白灰度 PNG，用于模拟"扫描件"（整页只有图片，无文本层）。"""
    raw = b"".join(b"\x00" + b"\xff" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8bit 灰度
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


FONT = "china-s"  # PyMuPDF 内置简体中文字体
FONT_SIZE = 10


def write_pdf(
    path: str | Path, items: list[tuple[float, float, str]], *, font: float = FONT_SIZE
) -> Path:
    """按 (x, y, text) 列表写一份 PDF，用于构造单栏/双栏/混合版式。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    for x, y, text in items:
        page.insert_text((x, y), text, fontname=FONT, fontsize=font)
    doc.save(str(p))
    doc.close()
    return p


def write_scanned_pdf(path: str | Path) -> Path:
    """模拟扫描件：整页只有一张图片，没有文本层。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=_white_png())
    doc.save(str(p))
    doc.close()
    return p


def write_empty_pdf(path: str | Path) -> Path:
    """空白页：无文本也无图片。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    doc.save(str(p))
    doc.close()
    return p


# —— 合成简历内容（虚构人物，仅用于测试版式处理）——
# 注意：文本长度受页宽约束（A4 595pt），左右栏文本都不得越过中缝，否则合成出的
# "分栏"在坐标投影上会重叠，无法用于验证分栏检测。
SINGLE_COLUMN_ITEMS: list[tuple[float, float, str]] = [
    (50, 60, "个人简历"),
    (50, 90, "姓名：李示例"),
    (50, 110, "联系电话：13800000000 邮箱：example@mail.com"),
    (50, 140, "求职意向：AI 应用开发工程师"),
    (50, 170, "教育背景"),
    (50, 195, "2018.09-2022.06 示例大学 计算机科学与技术 本科"),
    (50, 220, "主修课程：数据结构、机器学习、自然语言处理"),
    (50, 250, "工作经历"),
    (50, 275, "2022.07-至今 示例科技有限公司 算法工程师"),
    (50, 300, "负责 RAG 检索增强生成系统的设计与上线"),
    (50, 325, "独立微调过开源大模型并部署到生产环境"),
    (50, 350, "搭建数据清洗与效果评测流水线"),
    (50, 380, "项目经历"),
    (50, 405, "企业知识库问答助手：基于向量检索实现多轮问答"),
    (50, 430, "上线后问答准确率由百分之六十提升到百分之八十五"),
    (50, 460, "技能：Python、PyTorch、RAG、Agent、Docker"),
    (50, 485, "自我评价：动手能力强，习惯用数据验证效果"),
]

# 双栏：左栏为"工作经历"，右栏为"项目经历"，中间留 70pt 以上空白带
TWO_COLUMN_ITEMS: list[tuple[float, float, str]] = [
    (50, 60, "个人简历：李示例 联系电话：13800000000 邮箱：example@mail.com"),
    (50, 110, "工作经历"),
    (50, 140, "2022.07-至今 示例科技"),
    (50, 170, "算法工程师"),
    (50, 200, "工作描述一：负责 RAG 系统上线"),
    (50, 230, "工作描述二：独立完成模型微调"),
    (50, 260, "工作描述三：搭建数据评测流水线"),
    (330, 110, "项目经历"),
    (330, 140, "智能问答助手 2023.03"),
    (330, 170, "项目描述一：基于向量检索"),
    (330, 200, "项目描述二：多轮意图识别"),
    (330, 230, "项目描述三：准确率提升到 85%"),
    (330, 260, "项目描述四：负责灰度发布"),
    (50, 320, "教育背景：2018.09-2022.06 示例大学 计算机科学与技术 本科"),
]


@pytest.fixture
def single_column_pdf(tmp_path) -> Path:
    return write_pdf(tmp_path / "single.pdf", SINGLE_COLUMN_ITEMS)


@pytest.fixture
def two_column_pdf(tmp_path) -> Path:
    return write_pdf(tmp_path / "two_column.pdf", TWO_COLUMN_ITEMS)


@pytest.fixture
def scanned_pdf(tmp_path) -> Path:
    return write_scanned_pdf(tmp_path / "scanned.pdf")


@pytest.fixture
def empty_pdf(tmp_path) -> Path:
    return write_empty_pdf(tmp_path / "empty.pdf")
