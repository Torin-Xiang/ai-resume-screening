"""模型客户端工厂：换模型 = 改 provider / base_url / model，核心逻辑不动（方案 §4.1）。"""

from __future__ import annotations

import os
from typing import Any

from resume_screening.core.llm import LLMClient

PROVIDER_QWEN = "qwen"
PROVIDER_FAKE = "fake"  # 仅供测试与离线演练


def build_llm_client(
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    **kwargs: Any,
) -> LLMClient:
    """按 provider 构造客户端。默认读环境变量 LLM_PROVIDER / LLM_MODEL。"""
    provider = (provider or os.getenv("LLM_PROVIDER") or PROVIDER_QWEN).lower()

    if provider == PROVIDER_FAKE:
        from .fake import FakeLLMClient

        return FakeLLMClient(model=model or "fake-1", **kwargs)

    if provider in (PROVIDER_QWEN, "dashscope", "qwen-plus", "qwen-max", "qwen-turbo"):
        from .qwen import DEFAULT_MODEL, QwenClient

        return QwenClient(
            api_key=api_key,
            model=model or os.getenv("LLM_MODEL") or DEFAULT_MODEL,
            base_url=base_url or os.getenv("DASHSCOPE_BASE_URL") or None,
        )

    raise ValueError(f"未知的模型 provider：{provider}（可选：qwen / fake）")
