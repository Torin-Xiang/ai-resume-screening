"""解析/提取结果缓存（方案 §4.1、§6.2）。

目的：业务口径变化（换评分卡）时**只重跑评分层**，不重复解析与提取——省时间、省 token。
缓存键 = resume_id（文件内容 hash）；反序列化失败视为未命中（安全降级重跑）。

键里**不含提取器版本与模型标识**，所以命中时还要比对 `doc.extractor`：
只有"同一段原文 + 同一版提取逻辑 + 同一个模型"才算命中。
少了这道比对，升级解析规则或换个模型之后都会**静默复用旧结果**——
表现是"代码明明改了，跑出来还是老样子"，而且完全看不出是缓存干的。
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import ResumeDocument


class ResumeCache:
    """极简 JSON 文件缓存（MVP 承担的技术债：将来可换 SQLite）。"""

    def __init__(self, cache_dir: str | Path = "data/cache") -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, resume_id: str) -> Path:
        return self.dir / f"{resume_id}.json"

    def hit(self, resume_id: str, *, expect_extractor: str | None = None) -> ResumeDocument | None:
        """读缓存。`expect_extractor` 与文档记录的不一致时**视为未命中**。

        传 None 表示不校验版本（只判断有没有这份缓存）。
        """
        p = self.path_for(resume_id)
        if not p.exists():
            return None
        try:
            doc = ResumeDocument.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存损坏/版本不兼容 → 视为未命中
            return None
        if expect_extractor is not None and doc.extractor != expect_extractor:
            return None
        return doc

    def put(self, doc: ResumeDocument) -> Path:
        p = self.path_for(doc.resume_id)
        p.write_text(
            json.dumps(doc.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return p

    def __len__(self) -> int:
        return len(list(self.dir.glob("*.json")))
