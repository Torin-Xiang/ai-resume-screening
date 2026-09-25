"""LLM 统一入口（方案 §6.3）。

约束：**禁止各模块各自裸调 SDK**。所有 prompt 走这里：
- 统一重试（适配器内部用 tenacity）
- 统一成本记录（MeteredLLM 统计调用次数与 token）
- 统一换模型（换适配器即可，核心逻辑不动）

核心只定义协议与计量包装，具体实现放 `adapters/llm/`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Usage:
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    payload: dict[str, Any]
    usage: Usage = field(default_factory=Usage)
    raw: str = ""


@runtime_checkable
class LLMClient(Protocol):
    """模型客户端协议。适配器实现之；测试用 FakeLLMClient。"""

    name: str

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """返回**已校验为 JSON** 的结果；适配器负责 structured output 或解析兜底。"""
        ...


class MeteredLLM:
    """计量包装：统计调用次数与 token（用于成本记录与验收报告）。"""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.call_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "unknown")

    def complete_json(self, **kwargs: Any) -> LLMResponse:
        resp = self._inner.complete_json(**kwargs)
        self.call_count += 1
        self.prompt_tokens += resp.usage.prompt_tokens
        self.completion_tokens += resp.usage.completion_tokens
        return resp

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def snapshot(self) -> dict[str, int]:
        return {
            "calls": self.call_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }
