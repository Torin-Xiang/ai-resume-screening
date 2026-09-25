"""运行结果存储（MVP：本地 JSON；技术债：将来换 SQLite）。"""

from __future__ import annotations

import json
from pathlib import Path

from .models import RunRecord


def save_run(run: RunRecord, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return p


def load_run(path: str | Path) -> RunRecord:
    return RunRecord.model_validate_json(Path(path).read_text(encoding="utf-8"))


def default_run_path(run_id: str, root: str | Path = "data/runs") -> Path:
    return Path(root) / run_id / "run.json"
