"""通用 OpenAI 兼容端点客户端（DeepSeek / DashScope / 自建网关通吃）。

方案 §4.1：**换模型 = 改 provider / base_url / model，核心逻辑不动。**
这几家都提供 OpenAI 兼容的 `/chat/completions`，实际差异只有三处——
base_url、模型名、密钥从哪个环境变量读。把这三项参数化，就不必为每个厂商
抄一遍适配器；`qwen.py` 也因此退化成"通用客户端 + DashScope 默认值"。

**为什么不把这些直接写进 `qwen.py`**：那个文件的名字承诺了"只服务 DashScope"。
接入 DeepSeek 时若直接改它，`QwenClient` 就会开始读 DeepSeek 的密钥变量，
名字与行为对不上；后来人看到 `qwen.py` 也想不到 DeepSeek 藏在里面。

**密钥来源按「显式传入 > 环境变量（按优先级）」解析，取不到一律明确报错、绝不猜测**
（方案 §9：模型不会凭空知道密钥，程序也不该假装知道）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from resume_screening.core.llm import LLMResponse, Usage

#: 通用通道认可的密钥环境变量名（按优先级）。
#: 通用通道要求用户显式配 `LLM_API_KEY`，**故意不回退到 `DASHSCOPE_API_KEY`**：
#: 把阿里云的密钥发给 DeepSeek 端点既是配置错误，也是一次无谓的凭据外泄。
GENERIC_ENV_KEY_NAMES: tuple[str, ...] = ("LLM_API_KEY",)


class MissingAPIKeyError(RuntimeError):
    """缺少模型 API Key。"""


def resolve_api_key(
    api_key: str | None = None,
    *,
    env_names: Sequence[str] = GENERIC_ENV_KEY_NAMES,
) -> str | None:
    """按「显式传入 > 环境变量（按顺序）」取密钥；取不到返回 `None`。

    空白字符串等同于未配置——`.env` 里写了 `LLM_API_KEY=`（留空）是常见手误，
    把它当作"已配置"会把报错点推迟到第一次请求，离真正的原因很远。
    """
    if api_key and api_key.strip():
        return api_key.strip()
    for name in env_names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def key_env_hint(env_names: Sequence[str]) -> str:
    """拼出"该配哪个变量"的文案，供报错使用。"""
    return " 或 ".join(env_names) if env_names else "（无需密钥）"


def parse_json_payload(content: str) -> dict[str, Any]:
    """解析模型返回的 JSON。

    模型偶发把 JSON 包在 ```json 代码块里，此处做兜底剥离；
    但**不放任非法 JSON 静默通过**——解析失败就抛，由上层记为失败，
    免得把半截内容当成有效结论继续往下走。
    """
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


class OpenAICompatClient:
    """OpenAI 兼容的 JSON 补全客户端。

    统一在这里做三件事（方案 §6.3「禁止各模块裸调 SDK」）：
    重试、取 usage、剥 JSON 包装。子类只负责提供默认值。
    """

    def __init__(
        self,
        *,
        name: str,
        model: str,
        base_url: str,
        api_key: str | None = None,
        env_names: Sequence[str] = GENERIC_ENV_KEY_NAMES,
        max_retries: int = 3,
        timeout: float = 60.0,
    ) -> None:
        try:
            from openai import OpenAI  # 延迟导入：核心库与 CLI 不强制依赖 SDK
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("未安装 openai SDK：pip install openai") from e

        key = resolve_api_key(api_key, env_names=env_names)
        if not key:
            raise MissingAPIKeyError(
                f"缺少模型密钥（{key_env_hint(env_names)}）：请在项目根目录 .env 中配置，"
                f"例如 {env_names[0]}=...（MVP 不做密钥托管，缺失即明确报错，不猜测）"
            )

        self.name = name
        self.model = model
        self.base_url = base_url
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
        # 兼容端点普遍支持 json_object。注意 DeepSeek 的 JSON 输出模式要求
        # prompt 里出现 "json" 字样，项目各 prompt 均已满足（见 core/prompts.py）。
        #
        # `json_schema` 是 `LLMClient` 协议的参数，这里**刻意不使用**：
        # schema 已经以文本形式拼进 prompt（`EXTRACT_JSON_SCHEMA_HINT`），
        # 而各兼容端点对 `json_schema` 的支持程度不一致，传了反而会在某些
        # 端点上直接 400。保留参数是为了不破坏协议与调用方。
        kwargs["response_format"] = {"type": "json_object"}

        resp = self._client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or "{}"
        payload = parse_json_payload(content)
        usage = Usage(model=self.model)
        if getattr(resp, "usage", None):
            usage.prompt_tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
            usage.completion_tokens = getattr(resp.usage, "completion_tokens", 0) or 0
        return LLMResponse(payload=payload, usage=usage, raw=content)
