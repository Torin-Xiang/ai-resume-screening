"""`resume-screen serve`：启动网页工作台的入口。

为什么这也要测——产品方案 §1 认定 v0.1 失败的首因是"步骤太多、步骤不明了、
有使用门槛"。启动命令是用户碰到的第一道门，它出错或不说人话，前面做得再好也白搭。
所以这里钉住三件事：

1. 参数解析的优先级（命令行 > 环境变量 > 默认值），以及 `--lan` 的地址推导
2. **端口被占要给出可执行的下一步**，而不是把 uvicorn 堆栈糊到用户脸上
3. 三种"能启动但用不了"的情况必须有提示：前端没构建、没配 API key、以及
   局域网地址要打得出来（用户明确提到要能把链接复制到别的电脑用）

`uvicorn.run` 那一行不测——它是库的行为，测它等于测 uvicorn。
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from resume_screening.cli.main import app
from resume_screening.cli.serve_cmd import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    LAN_HOST,
    PortInUse,
    ensure_port_free,
    lan_addresses,
    missing_api_key,
    missing_key_help,
    preflight_warnings,
    resolve_settings,
)

# ——————————————————— 参数解析 ———————————————————

def test_defaults_bind_to_localhost_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认只绑本机。单机单人阶段不该在用户不知情时把服务暴露到局域网。"""
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    settings = resolve_settings()
    assert settings.host == DEFAULT_HOST
    assert settings.port == DEFAULT_PORT
    assert settings.url == f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/"


def test_env_used_when_cli_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9123")
    settings = resolve_settings()
    assert settings.port == 9123


def test_cli_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """命令行显式给的参数必须压过环境变量，否则用户改了没反应会以为是 bug。"""
    monkeypatch.setenv("PORT", "9123")
    assert resolve_settings(port=9999).port == 9999


def test_lan_flag_binds_all_interfaces() -> None:
    settings = resolve_settings(lan=True)
    assert settings.host == LAN_HOST


def test_explicit_host_beats_lan_flag() -> None:
    """`--host` 比 `--lan` 更具体，冲突时以 `--host` 为准。"""
    assert resolve_settings(host="127.0.0.1", lan=True).host == "127.0.0.1"


def test_lan_url_points_at_localhost_not_zero() -> None:
    """绑 0.0.0.0 时，本机该打开的地址是 127.0.0.1——
    让人去点 `http://0.0.0.0:8000` 是很常见的误导。"""
    settings = resolve_settings(lan=True)
    assert settings.url == f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/"


def test_data_dir_is_resolved_to_absolute(tmp_path: Path) -> None:
    settings = resolve_settings(data_dir=str(tmp_path))
    assert settings.data_dir is not None and settings.data_dir.is_absolute()


# ——————————————————— 端口检查 ———————————————————

def test_ensure_port_free_raises_with_actionable_context() -> None:
    """端口被占时报错要带上 host/port 与"换端口"的出路。"""
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind((DEFAULT_HOST, 0))
    occupied.listen(1)
    port = occupied.getsockname()[1]
    try:
        with pytest.raises(PortInUse) as excinfo:
            ensure_port_free(DEFAULT_HOST, port)
        assert str(port) in str(excinfo.value)
    finally:
        occupied.close()


def test_ensure_port_free_passes_for_free_port() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((DEFAULT_HOST, 0))
    port = probe.getsockname()[1]
    probe.close()
    ensure_port_free(DEFAULT_HOST, port)  # 不抛异常即通过


def test_lan_addresses_exclude_loopback() -> None:
    """局域网链接不能是 127.0.0.1，否则复制到别的电脑必然打不开。"""
    assert all(not address.startswith("127.") for address in lan_addresses())


# ——————————————————— 前置检查 ———————————————————

def test_warns_when_frontend_not_built(tmp_path: Path) -> None:
    warnings = preflight_warnings(ui_dist=tmp_path / "dist")
    assert any("npm run build" in warning for warning in warnings)


def test_no_frontend_warning_when_built(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    assert preflight_warnings(ui_dist=dist) == []


def test_missing_api_key_detected_for_real_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert missing_api_key("qwen") is True


def test_missing_api_key_not_reported_for_fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """fake 是离线演练通道，要求它配密钥是错的。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert missing_api_key("fake") is False


def test_missing_api_key_false_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-not-a-real-key")
    assert missing_api_key("qwen") is False


def test_missing_api_key_follows_the_active_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥变量名由 provider 决定，不能写死。

    本模块原本硬编码 `DASHSCOPE_API_KEY`。接入 DeepSeek 后，用户配好了
    `LLM_API_KEY`，预检却仍判"缺密钥"并拒绝启动——对着一个配置完全正确的
    用户报错，是最容易让人放弃的那类 bug。
    """
    monkeypatch.setenv("LLM_API_KEY", "sk-not-a-real-key")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    assert missing_api_key("deepseek") is False
    assert missing_api_key("qwen") is True


def test_missing_key_help_names_the_variable_of_the_active_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提示里要写用户当前该配的那个变量，而不是永远那句 DASHSCOPE_API_KEY。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    assert "LLM_API_KEY" in missing_key_help("deepseek")
    assert "DASHSCOPE_API_KEY" in missing_key_help("qwen")
    # 两条出路都要给出：补密钥，或者先用离线模式看界面
    for provider in ("deepseek", "qwen"):
        assert "LLM_PROVIDER=fake" in missing_key_help(provider)


def test_missing_key_help_points_at_provider_when_it_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 写错时不该说"缺密钥"——那是两码事，会把排查引到错误方向。"""
    help_text = missing_key_help("typo-provider")

    assert "typo-provider" in help_text
    assert "LLM_PROVIDER" in help_text
    assert "DASHSCOPE_API_KEY" not in help_text


def test_missing_key_stops_serve_with_actionable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺密钥时服务其实起不来（客户端构造即抛错）。

    曾经的行为是先打印"已启动"、紧接着一个 Traceback——两句话互相矛盾，
    用户只会认为程序坏了。现在必须在启动前拦住，并把两条出路写清楚。
    """
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    result = CliRunner().invoke(app, ["serve", "--port", "8231"])

    assert result.exit_code == 1, result.output
    assert "DASHSCOPE_API_KEY" in result.output
    assert "LLM_PROVIDER=fake" in result.output
    # 不能把库的堆栈直接甩给用户
    assert "Traceback" not in result.output
    # 更不能先说"已启动"
    assert "已启动" not in result.output


# ——————————————————— 命令注册 ———————————————————

def test_serve_command_is_registered() -> None:
    """`serve` 必须挂在主命令下，否则用户按文档敲就是 "No such command"。"""
    result = CliRunner().invoke(app, ["serve", "--help"])
    assert result.exit_code == 0, result.output
    for option in ("--host", "--port", "--data-dir", "--concurrency", "--lan", "--open"):
        assert option in result.output


def test_top_level_help_mentions_serve() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "serve" in result.output


# ——————————————————— 自动打开浏览器 ———————————————————

def _free_port() -> int:
    """向系统要一个当前空闲的端口，避免测试之间互相抢同一个端口。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((DEFAULT_HOST, 0))
    port: int = probe.getsockname()[1]
    probe.close()
    return port


class _ImmediateTimer:
    """把 `threading.Timer` 换成"立刻执行"，免得测试真等一秒。"""

    def __init__(self, interval: float, function, args=None, kwargs=None) -> None:
        self.function = function

    def start(self) -> None:
        self.function()


def test_open_flag_launches_the_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--open` 必须真的开浏览器。

    让用户自己从终端里复制网址，正是 v0.1 被否掉的"步骤太多、有使用门槛"。
    双击启动脚本能不能"点完就见到界面"，全靠这一个参数。
    """
    import resume_screening.cli.serve_cmd as serve_cmd
    import resume_screening.web.app as web_app

    opened: list[str] = []
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setattr(serve_cmd.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(serve_cmd.threading, "Timer", _ImmediateTimer)
    monkeypatch.setattr(web_app, "run_server", lambda *a, **k: None)

    port = _free_port()
    result = CliRunner().invoke(
        app, ["serve", "--port", str(port), "--data-dir", str(tmp_path), "--open"]
    )

    assert result.exit_code == 0, result.output
    assert opened == [f"http://127.0.0.1:{port}/"]


def test_without_open_flag_nothing_is_launched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """不加 `--open` 就不该开浏览器——脚本化/CI 场景静默启动是前提。"""
    import resume_screening.cli.serve_cmd as serve_cmd
    import resume_screening.web.app as web_app

    opened: list[str] = []
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setattr(serve_cmd.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(serve_cmd.threading, "Timer", _ImmediateTimer)
    monkeypatch.setattr(web_app, "run_server", lambda *a, **k: None)

    result = CliRunner().invoke(
        app, ["serve", "--port", str(_free_port()), "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert opened == []
