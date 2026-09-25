"""测试用模型客户端适配器：不联网、可预设返回，供核心逻辑单测使用（假适配器测试策略）。"""

from __future__ import annotations

from typing import Any, Callable

from resume_screening.core.llm import LLMResponse, Usage


class FakeLLMClient:
    """按 user prompt 关键字返回预设 payload。

    用法：
        FakeLLMClient(match={"----- 原文开始 -----": {...提取结果...}})
        FakeLLMClient(queue=[{...}, {...}])       # 按调用顺序返回
        FakeLLMClient(default={...})
    """

    name = "fake"

    def __init__(
        self,
        *,
        match: dict[str, Any] | None = None,
        queue: list[dict[str, Any]] | None = None,
        default: dict[str, Any] | None = None,
        model: str = "fake-1",
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
        recorder: Callable[[str, str], None] | None = None,
    ) -> None:
        self.match = match or {}
        self.queue = list(queue or [])
        self.default = default if default is not None else {}
        self.model = model
        self._usage = Usage(
            model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
        self.calls: list[dict[str, Any]] = []
        self._recorder = recorder

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        if self._recorder:
            self._recorder(system, user)
        payload: Any = None
        for key, value in self.match.items():
            if key in user or key in system:
                payload = value
                break
        if payload is None and self.queue:
            payload = self.queue.pop(0)
        if payload is None:
            payload = self.default
        if callable(payload):
            payload = payload(user)
        return LLMResponse(payload=payload, usage=self._usage, raw=str(payload))

    @property
    def call_count(self) -> int:
        return len(self.calls)
