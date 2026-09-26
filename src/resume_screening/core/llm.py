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


# ——————————————————— 错误翻译 ———————————————————

def humanize_llm_error(exc: BaseException) -> str | None:
    """把模型调用异常翻译成 HR 能行动的一句话；认不出就返回 None（走原样显示）。

    为什么不直接 `str(exc)`：openai SDK 的异常体是一坨 JSON，
    「Insufficient Balance」埋在里面，HR 看不出下一步该干嘛。
    而这几类错误的"下一步"是有限集合：充值 / 查密钥 / 等重试 / 查网络。
    刻意不做异常类型匹配（不 import openai）——按特征认，核心层不绑 SDK。
    """
    text = str(exc)
    status = getattr(exc, "status_code", None)

    if "insufficient balance" in text.lower() or status == 402:
        return (
            "模型服务余额不足：去服务商控制台充值后，重新上传这份简历即可"
            "（其余简历不受影响）"
        )
    if status == 401 or "invalid api key" in text.lower() or "unauthorized" in text.lower():
        return "模型服务密钥无效：检查 .env 里的密钥是否正确、有没有删过或过期"
    if status == 429:
        return "模型服务限流：系统已自动重试仍失败，稍等几分钟后重新上传即可"
    if "timeout" in text.lower() or "timed out" in text.lower():
        return "连接模型服务超时：检查网络后重新上传即可"
    if "connection" in text.lower() and "error" in text.lower():
        return "连不上模型服务：检查网络或服务地址（base_url）是否正确"
    return None
