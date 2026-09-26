"""通义千问模型客户端适配器（DashScope OpenAI 兼容端点）。

方案 §6.3：禁止各模块裸调 SDK；换模型只需换适配器。

本适配器现在只是**通用 OpenAI 兼容客户端 + DashScope 的默认值与密钥变量名**，
公共逻辑（重试 / usage / JSON 兜底）都在 `openai_compat.py`。
构造签名与迁移前保持一致，调用方无需改动。
"""

from __future__ import annotations

from .openai_compat import MissingAPIKeyError, OpenAICompatClient

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"

#: DashScope 通道只认自己的密钥变量名。
#: **刻意不把 `LLM_API_KEY` 列为回退**——那个变量属于通用通道（DeepSeek 等），
#: 回退会把别的厂商的密钥发给阿里云。
ENV_KEY_NAMES: tuple[str, ...] = ("DASHSCOPE_API_KEY",)


class QwenClient(OpenAICompatClient):
    """DashScope 通道客户端。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(
            name="qwen",
            model=model,
            base_url=base_url or DEFAULT_BASE_URL,
            api_key=api_key,
            env_names=ENV_KEY_NAMES,
            max_retries=max_retries,
            timeout=timeout,
        )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "ENV_KEY_NAMES",
    "MissingAPIKeyError",
    "QwenClient",
]
