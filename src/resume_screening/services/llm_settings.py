"""模型通道的运行时选择（界面"切换模型"的后半段）。

职责边界：本模块**只负责"选哪个通道"这件事**——候选清单、持久化、按选择
装配客户端。它不认识 FastAPI（分层约束），也不做任何模型调用；切换后真正
失败（如余额不足）发生在处理简历时，由 `humanize_llm_error` 翻译成人话。

持久化是一个几行的 JSON 文件（`data_dir/llm_config.json`）：切换通道是
用户级偏好，不值得为此建表；但它又必须**跨重启生效**——不然每次重启
都悄悄退回 .env 默认，用户以为还在用真实模型，实际已经变回离线。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..adapters.llm.factory import (
    PROVIDER_DEEPSEEK,
    PROVIDER_SCRIPTED,
    build_llm_client,
    has_api_key,
)
from ..core.llm import LLMClient

CONFIG_FILE = "llm_config.json"

#: 界面上可供切换的通道。刻意只有三个：离线（0 token）+ DeepSeek 两个档位。
#: 加通道 = 在这里加一行，界面与接口自动跟上。
LLM_CHOICES: list[dict[str, str]] = [
    {
        "provider": PROVIDER_SCRIPTED,
        "model": "scripted-1",
        "label": "离线（规则判定，不消耗 token）",
        "needs_key": "",
    },
    {
        "provider": PROVIDER_DEEPSEEK,
        "model": "deepseek-flash",
        "label": "DeepSeek flash（便宜，语义判定）",
        "needs_key": "LLM_API_KEY",
    },
    {
        "provider": PROVIDER_DEEPSEEK,
        "model": "deepseek-v4-pro",
        "label": "DeepSeek v4-pro（效果最好）",
        "needs_key": "LLM_API_KEY",
    },
]


def config_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / CONFIG_FILE


def load_choice(data_dir: str | Path) -> dict[str, str] | None:
    """读上次保存的通道选择。文件缺失/损坏/字段不全 → None（回退 .env 默认）。"""
    path = config_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    provider = str(data.get("provider") or "").strip()
    model = str(data.get("model") or "").strip()
    if not provider or not model:
        return None
    return {"provider": provider, "model": model}


def save_choice(data_dir: str | Path, *, provider: str, model: str) -> None:
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"provider": provider, "model": model}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_client(choice: dict[str, str] | None) -> LLMClient:
    """按选择装配客户端；choice 为 None 时走 .env 默认（factory 的职责）。"""
    if choice is None:
        return build_llm_client()
    return build_llm_client(provider=choice["provider"], model=choice["model"])


def choice_is_available(item: dict[str, str]) -> bool:
    """该通道当前是否可切：需要密钥的通道，密钥缺失就置灰。

    密钥读**进程环境变量**（CLI 启动时已从 .env 载入），所以改了 .env
    仍需重启服务才会被这里看见——与 provider 在启动时解析的现状一致。
    """
    key_name = item.get("needs_key") or ""
    return not key_name or os.getenv(key_name) is not None


def validate(provider: str, model: str | None) -> tuple[dict[str, str], None] | tuple[None, str]:
    """校验一次切换请求。返回 (choice, None) 或 (None, 错误信息)。"""
    matched = [c for c in LLM_CHOICES if c["provider"] == provider]
    if not matched:
        known = "、".join(sorted({c["provider"] for c in LLM_CHOICES}))
        return None, f"不支持的模型通道：{provider}（可选：{known}）"
    if model:
        matched = [c for c in matched if c["model"] == model] or matched
    item = matched[0]
    if not choice_is_available(item):
        return None, f"该通道需要配置 {item['needs_key']}（写在 .env 后重启服务生效）"
    return {"provider": item["provider"], "model": item["model"]}, None


def snapshot() -> dict[str, Any]:
    """给界面的候选清单：全部通道 + 各自当前是否可选。"""
    return {
        "choices": [
            {**item, "available": choice_is_available(item)} for item in LLM_CHOICES
        ]
    }
