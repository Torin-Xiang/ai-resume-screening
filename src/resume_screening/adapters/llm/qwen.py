"""通义千问模型客户端适配器（DashScope OpenAI 兼容端点）。

方案 §6.3：禁止各模块裸调 SDK；换模型只需换适配器。
API key 从 .env 的 DASHSCOPE_API_KEY 读取（缺失时明确报错，不猜测、不虚构）。
"""

from __future__ import annotations

import json
import os
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from resume_screening.core.llm import LLMResponse, Usage

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"


class MissingAPIKeyError(RuntimeError):
    """缺少 DASHSCOPE_API_KEY。"""


class QwenClient:
    name = "qwen"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = 3,
        timeout: float = 60.0,
    ) -> None:
        try:
            from openai import OpenAI  # 延迟导入：核心库与 CLI 不强制依赖 SDK
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("未安装 openai SDK：pip install openai") from e

        key = api_key or os.getenv("DASHSCOPE_API_KEY")
        if not key:
            raise MissingAPIKeyError(
                "缺少 DASHSCOPE_API_KEY：请在项目根目录 .env 中配置 DASHSCOPE_API_KEY=... "
                "（MVP 不做密钥托管，缺失即明确报错，不猜测）"
            )
        self.model = model
        self._max_retries = max_retries
        self._client = OpenAI(api_key=key, base_url=base_url, timeout=timeout)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def complete_json(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        # DashScope 兼容端点支持 json_object；json_schema 仅在显式传入时附加
        kwargs["response_format"] = {"type": "json_object"}

        resp = self._client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or "{}"
        payload = self._parse_json(content)
        usage = Usage(model=self.model)
        if getattr(resp, "usage", None):
            usage.prompt_tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
            usage.completion_tokens = getattr(resp.usage, "completion_tokens", 0) or 0
        return LLMResponse(payload=payload, usage=usage, raw=content)

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        """模型偶发包裹 ```json 代码块，此处做兜底剥离（不放任非法 JSON 静默通过）。"""
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
