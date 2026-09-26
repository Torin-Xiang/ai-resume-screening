"""模型通道：DeepSeek / 通用 OpenAI 兼容端点。

背景：项目最初只接了通义千问（`DASHSCOPE_API_KEY` + 固定 DashScope 端点）。
接入 DeepSeek 时暴露两个问题，本文件把它们锁住：

1. **配置变量名不同**（`LLM_API_KEY` / `LLM_BASE_URL`），工厂不认识 → 用户按
   直觉配了却收到一句「缺少 DASHSCOPE_API_KEY」，报错指向与他配置无关的地方。
2. **密钥来源必须隔离**：qwen 通道只认 `DASHSCOPE_API_KEY`，通用通道只认
   `LLM_API_KEY`。放任互相回退，等于把阿里云的密钥发给 DeepSeek 端点——
   既是配置错误，也是一次无谓的凭据外泄。

对应方案 §4.1「换模型 = 改 provider / base_url / model，核心逻辑不动」。
"""

from __future__ import annotations

import pytest

from resume_screening.adapters.llm.factory import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    KNOWN_PROVIDERS,
    build_llm_client,
    env_names_for,
    has_api_key,
    resolve_provider,
)
from resume_screening.adapters.llm.openai_compat import (
    MissingAPIKeyError,
    OpenAICompatClient,
    resolve_api_key,
)
from resume_screening.adapters.llm.qwen import QwenClient

FAKE_KEY = "sk-test-not-a-real-key"


# ——————————————————— provider 解析 ———————————————————


def test_deepseek_provider_uses_llm_api_key_and_builtin_endpoint(monkeypatch) -> None:
    """`LLM_PROVIDER=deepseek` 时，端点与默认模型都内置，用户只需配 key。"""
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)

    client = build_llm_client()

    assert isinstance(client, OpenAICompatClient)
    assert client.name == "deepseek"
    assert client.base_url == DEEPSEEK_BASE_URL
    assert client.model == DEEPSEEK_DEFAULT_MODEL


def test_deepseek_respects_llm_model_override(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_MODEL", "deepseek-reasoner")

    assert build_llm_client().model == "deepseek-reasoner"


def test_generic_provider_requires_base_url(monkeypatch) -> None:
    """通用通道没有"内置端点"这回事，缺 base_url 必须明确报错而不是猜一个。"""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)

    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        build_llm_client()


def test_generic_provider_requires_model(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")

    with pytest.raises(ValueError, match="LLM_MODEL"):
        build_llm_client()


def test_generic_provider_builds_client_from_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "some-model")

    client = build_llm_client()

    assert client.name == "openai"
    assert client.base_url == "https://example.invalid/v1"
    assert client.model == "some-model"


def test_provider_inferred_from_base_url_when_unset(monkeypatch) -> None:
    """没写 LLM_PROVIDER 但配了 LLM_BASE_URL → 按通用通道走。

    否则用户会收到一句「缺少 DASHSCOPE_API_KEY」——他说的是 DeepSeek，
    报错却提到 DashScope，只会让人以为是程序认错了配置。
    """
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "some-model")

    assert resolve_provider() == "openai"
    assert build_llm_client().name == "openai"


def test_provider_defaults_to_qwen_without_any_hint(monkeypatch) -> None:
    """三个变量都没配时保持历史行为（qwen），不引入隐性默认。"""
    for name in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    assert resolve_provider() == "qwen"


def test_unknown_provider_error_lists_valid_choices(monkeypatch) -> None:
    """provider 写错时要说清有哪些合法取值，否则用户只能去翻源码。"""
    monkeypatch.setenv("LLM_PROVIDER", "gpt-5-from-the-future")

    with pytest.raises(ValueError) as excinfo:
        build_llm_client()

    message = str(excinfo.value)
    assert "gpt-5-from-the-future" in message
    for known in ("deepseek", "qwen", "fake"):
        assert known in message
        assert known in KNOWN_PROVIDERS


# ——————————————————— 密钥来源隔离 ———————————————————


def test_generic_channel_does_not_borrow_dashscope_key(monkeypatch) -> None:
    """通用通道不许回退到 DASHSCOPE_API_KEY。

    回退看起来"更方便"，实际是把阿里云的密钥发给第三方端点。
    宁可报错让人补一行配置，也不能静默把凭据发错地方。
    """
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)

    with pytest.raises(MissingAPIKeyError, match="LLM_API_KEY"):
        build_llm_client()


def test_qwen_channel_does_not_borrow_generic_key(monkeypatch) -> None:
    """反向同理：qwen 通道不许读 LLM_API_KEY。"""
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)

    with pytest.raises(MissingAPIKeyError, match="DASHSCOPE_API_KEY"):
        build_llm_client()


def test_explicit_api_key_wins_over_env(monkeypatch) -> None:
    """显式传入的 key 优先于环境变量（供脚本里临时换 key 使用）。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-from-env")

    assert resolve_api_key("sk-explicit", env_names=("LLM_API_KEY",)) == "sk-explicit"


def test_blank_env_value_counts_as_missing(monkeypatch) -> None:
    """`.env` 里写了 `LLM_API_KEY=`（留空）是常见手误。

    把它当成"已配置"，报错会推迟到第一次请求，离真正的原因很远。
    """
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_API_KEY", "   ")

    assert has_api_key() is False
    with pytest.raises(MissingAPIKeyError):
        build_llm_client()


def test_surrounding_whitespace_in_key_is_trimmed(monkeypatch) -> None:
    """从网页复制密钥常带首尾空白，粘进 .env 后不该让请求 401。"""
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_API_KEY", f"  {FAKE_KEY}  ")

    assert build_llm_client() is not None


# ——————————————————— 密钥需求表（serve 预检依赖它）———————————————————


def test_env_names_per_channel(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    assert env_names_for() == ("DASHSCOPE_API_KEY",)

    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    assert env_names_for() == ("LLM_API_KEY",)

    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert env_names_for() == ()


def test_has_api_key_true_for_fake_and_deepseek(monkeypatch) -> None:
    """fake 不需要密钥；深层通道配了就为真。"""
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert has_api_key() is True

    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    assert has_api_key() is True


def test_has_api_key_does_not_raise_for_unknown_provider(monkeypatch) -> None:
    """`has_api_key` 服务于服务启动前的预检，那里抛错只会变成一句无关的堆栈。

    未知 provider 的报错交给 `build_llm_client`——它才是能列出合法取值的地方。
    """
    monkeypatch.setenv("LLM_PROVIDER", "typo-provider")

    assert has_api_key() is True  # 不拦；随后的装配阶段会给出清晰报错
    with pytest.raises(ValueError, match="typo-provider"):
        build_llm_client()


def test_qwen_client_signature_unchanged(monkeypatch) -> None:
    """`QwenClient` 的构造签名在重构后必须保持兼容（调用方无需改动）。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_KEY)

    client = QwenClient()
    assert client.name == "qwen"
    assert client.model == "qwen-plus"
    assert isinstance(client, OpenAICompatClient)
