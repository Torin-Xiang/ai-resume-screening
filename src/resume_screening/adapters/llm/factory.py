"""模型客户端工厂：换模型 = 改 provider / base_url / model，核心逻辑不动（方案 §4.1）。

支持的通道：

| provider | 适配器 | 密钥变量 | 默认端点 |
|---|---|---|---|
| `qwen`（含 `dashscope` 等别名） | `QwenClient` | `DASHSCOPE_API_KEY` | DashScope 兼容端点 |
| `deepseek` | `OpenAICompatClient` | `LLM_API_KEY` | `api.deepseek.com/v1` |
| `openai`（通用兼容） | `OpenAICompatClient` | `LLM_API_KEY` | 必须由 `LLM_BASE_URL` 指定 |
| `fake` | `FakeLLMClient` | 不需要 | 离线，仅供测试与演练 |

**provider 的解析顺序**：CLI 参数 > `LLM_PROVIDER` > 按 `LLM_BASE_URL`/`LLM_API_KEY`
推断 > 默认 `qwen`。之所以要做那一步推断：那两个变量只在"接非 DashScope 的兼容端点"
时才需要配，用户配了却忘了写 `LLM_PROVIDER` 时，会收到一句莫名其妙的
「缺少 DASHSCOPE_API_KEY」——报错指向了与他配置无关的地方。
"""

from __future__ import annotations

import os
from typing import Any

from resume_screening.core.llm import LLMClient

from .openai_compat import GENERIC_ENV_KEY_NAMES, OpenAICompatClient, resolve_api_key

PROVIDER_QWEN = "qwen"
PROVIDER_FAKE = "fake"  # 仅供测试与离线演练
PROVIDER_SCRIPTED = "scripted"  # 离线脚本模型：规则驱动，看输出形态用
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_OPENAI = "openai"  # 通用 OpenAI 兼容（自建网关 / vLLM / 其他厂商）

#: 走通用 OpenAI 兼容端点的 provider 名
GENERIC_PROVIDERS = frozenset(
    {PROVIDER_OPENAI, PROVIDER_DEEPSEEK, "openai-compatible", "compatible"}
)
#: DashScope 通道的 provider 别名（历史遗留：曾把模型名当 provider 用）
QWEN_PROVIDERS = frozenset({PROVIDER_QWEN, "dashscope", "qwen-plus", "qwen-max", "qwen-turbo"})

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"

KNOWN_PROVIDERS = sorted(GENERIC_PROVIDERS | QWEN_PROVIDERS | {PROVIDER_FAKE, PROVIDER_SCRIPTED})

#: 各 CLI 命令 `--provider` 选项共用的帮助文案。
#: 抽成常量是为了避免两处命令各写一份、接入新厂商时只改一处而漂移。
PROVIDER_OPTION_HELP = (
    "模型 provider：deepseek / qwen / openai / fake / scripted（默认读 LLM_PROVIDER）"
)


def resolve_provider(provider: str | None = None) -> str:
    """解析最终 provider（CLI 参数 > 环境变量 > 推断 > 默认）。"""
    explicit = (provider or os.getenv("LLM_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("LLM_BASE_URL") or os.getenv("LLM_API_KEY"):
        return PROVIDER_OPENAI
    return PROVIDER_QWEN


def env_names_for(provider: str | None = None) -> tuple[str, ...]:
    """该 provider 认可的密钥环境变量名（按优先级）；不需要密钥时返回空元组。

    未知 provider 返回空元组而非抛错：本函数要服务启动前的"缺密钥"检查，
    那里抛错会变成一句与原因无关的堆栈。未知 provider 的报错交给
    `build_llm_client`——它才是真正装配客户端、能说清合法取值的地方。
    """
    resolved = resolve_provider(provider)
    if resolved in (PROVIDER_FAKE, PROVIDER_SCRIPTED):
        return ()
    if resolved in QWEN_PROVIDERS:
        from .qwen import ENV_KEY_NAMES

        return ENV_KEY_NAMES
    if resolved in GENERIC_PROVIDERS:
        return GENERIC_ENV_KEY_NAMES
    return ()


def has_api_key(provider: str | None = None) -> bool:
    """该 provider 所需的密钥是否已配置。`fake` 恒为 True（离线不需要密钥）。"""
    names = env_names_for(provider)
    if not names:
        return True
    return resolve_api_key(None, env_names=names) is not None


def build_llm_client(
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    **kwargs: Any,
) -> LLMClient:
    """按 provider 构造客户端。默认读环境变量 LLM_PROVIDER / LLM_MODEL / LLM_BASE_URL。"""
    resolved = resolve_provider(provider)

    if resolved == PROVIDER_FAKE:
        from .fake import FakeLLMClient

        return FakeLLMClient(model=model or "fake-1", **kwargs)

    if resolved == PROVIDER_SCRIPTED:
        from .scripted import ScriptedLLMClient

        return ScriptedLLMClient(model=model or "scripted-1", **kwargs)

    if resolved in QWEN_PROVIDERS:
        from .qwen import DEFAULT_MODEL, QwenClient

        return QwenClient(
            api_key=api_key,
            model=model or os.getenv("LLM_MODEL") or DEFAULT_MODEL,
            base_url=base_url or os.getenv("DASHSCOPE_BASE_URL") or None,
            **kwargs,
        )

    if resolved in GENERIC_PROVIDERS:
        default_base = DEEPSEEK_BASE_URL if resolved == PROVIDER_DEEPSEEK else None
        resolved_base = base_url or os.getenv("LLM_BASE_URL") or default_base
        if not resolved_base:
            raise ValueError(
                f"provider={resolved} 需要 base_url：请设置 LLM_BASE_URL，"
                "或改用 provider=deepseek（默认端点已内置）"
            )
        default_model = DEEPSEEK_DEFAULT_MODEL if resolved == PROVIDER_DEEPSEEK else ""
        resolved_model = model or os.getenv("LLM_MODEL") or default_model
        if not resolved_model:
            raise ValueError(
                f"provider={resolved} 需要模型名：请在 .env 中设置 LLM_MODEL=..."
            )
        return OpenAICompatClient(
            name=resolved,
            api_key=api_key,
            model=resolved_model,
            base_url=resolved_base,
            **kwargs,
        )

    raise ValueError(
        f"未知的模型 provider：{resolved}（可选：{' / '.join(KNOWN_PROVIDERS)}）"
    )
