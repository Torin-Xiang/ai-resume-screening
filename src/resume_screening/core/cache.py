"""解析/提取结果缓存（方案 §4.1、§6.2）。

目的：业务口径变化（换评分卡）时**只重跑评分层**，不重复解析与提取——省时间、省 token。
缓存键 = resume_id（文件内容 hash），内容不变即命中；反序列化失败视为未命中（安全降级重跑）。
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

    def hit(self, resume_id: str) -> ResumeDocument | None:
        p = self.path_for(resume_id)
        if not p.exists():
            return None
        try:
            return ResumeDocument.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存损坏/版本不兼容 → 视为未命中
            return None

    def put(self, doc: ResumeDocument) -> Path:
        p = self.path_for(doc.resume_id)
        p.write_text(
            json.dumps(doc.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return p

    def __len__(self) -> int:
        return len(list(self.dir.glob("*.json")))
