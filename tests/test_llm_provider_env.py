"""回归：LLM_PROVIDER 环境变量必须生效。

缺陷背景：`screen` / `rubric` 的 `--provider` 选项硬编码默认值 "qwen"，
会遮蔽 .env 里的 LLM_PROVIDER，导致 README §环境变量 声明的
「LLM_PROVIDER=fake 离线演练」失效——用户按文档配了 fake，实际仍去要 API key。

方案 §3 约定 CLI 是薄壳、provider 选择属于适配器装配，因此 CLI 只传 None，
由 factory 统一按 env 解析。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from resume_screening.adapters.llm.fake import FakeLLMClient
from resume_screening.adapters.llm.factory import build_llm_client
from resume_screening.cli.main import app

RUBRIC_PATH = Path(__file__).resolve().parent.parent / "rubrics" / "example.json"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def no_api_key(monkeypatch):
    """确保环境里没有真实的 key，避免测试受本机 .env 影响。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)


def test_factory_fake_from_env(monkeypatch, no_api_key):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert isinstance(build_llm_client(provider=None), FakeLLMClient)


def test_factory_defaults_to_qwen_without_env(monkeypatch, no_api_key):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    from resume_screening.adapters.llm.qwen import QwenClient

    # 不传 provider 且无 env 时仍默认 qwen，但构造时缺 key 必须明确报错（不许猜）
    with pytest.raises(Exception):
        build_llm_client(provider=None)


def test_screen_uses_fake_provider_from_env(monkeypatch, no_api_key, runner, single_column_pdf, tmp_path):
    """按 README 配置 LLM_PROVIDER=fake 后，screen 应当能离线跑通，不索要 API key。"""
    monkeypatch.setenv("LLM_PROVIDER", "fake")

    input_dir = single_column_pdf.parent
    out = tmp_path / "out.xlsx"
    result = runner.invoke(
        app,
        [
            "screen",
            "--rubric",
            str(RUBRIC_PATH),
            "--input",
            str(input_dir),
            "--out",
            str(out),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--run-dir",
            str(tmp_path / "runs"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert out.exists()


def test_screen_without_key_and_without_fake_env_fails_loudly(monkeypatch, no_api_key, runner, single_column_pdf, tmp_path):
    """没有 key 也没有 fake 时，必须报错退出，而不是静默猜测或产出空结果。"""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    result = runner.invoke(
        app,
        [
            "screen",
            "--rubric",
            str(RUBRIC_PATH),
            "--input",
            str(single_column_pdf.parent),
            "--out",
            str(tmp_path / "out.xlsx"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--run-dir",
            str(tmp_path / "runs"),
        ],
    )

    assert result.exit_code != 0
