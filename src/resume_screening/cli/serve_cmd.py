"""`serve` —— 启动网页工作台（v1.0 的主界面入口）。

v1.0 的产品形态是网页，CLI 只剩一个职责：**点火**。
产品方案 §1 认定 v0.1 失败的原因是"步骤太多、步骤不明了、有使用门槛"，
所以这个命令要能自己把三件最容易让人卡住的事说清楚，而不是让人去翻文档：

1. 打开哪个网址（含 `--lan` 时给可复制到别的电脑的地址）
2. 端口被占了怎么办
3. 前端没构建 / 没配 API key 这两个"起来了但用不了"的情况

真正的启动逻辑只有 `uvicorn.run` 一行；其余都拆成纯函数，便于测试
（AGENT.md：交付前测试必须全绿）。
"""

from __future__ import annotations

import os
import socket
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import typer

DEFAULT_HOST = "127.0.0.1"
LAN_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


class PortInUse(RuntimeError):
    """端口已被占用。单独一个异常类型，是为了能给出"换一个端口"的具体建议。"""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(f"{host}:{port} 已被占用")
        self.host = host
        self.port = port


@dataclass(frozen=True)
class ServeSettings:
    host: str
    port: int
    data_dir: Path | None
    concurrency: int | None

    @property
    def url(self) -> str:
        """浏览器里要打开的地址。绑 0.0.0.0 时本机该访问 127.0.0.1。"""
        display = DEFAULT_HOST if self.host == LAN_HOST else self.host
        return f"http://{display}:{self.port}/"


def resolve_settings(
    *,
    host: str | None = None,
    port: int | None = None,
    data_dir: str | Path | None = None,
    concurrency: int | None = None,
    lan: bool = False,
) -> ServeSettings:
    """合并命令行参数、环境变量与默认值。命令行 > 环境变量 > 默认值。"""
    resolved_host = host or (LAN_HOST if lan else None) or os.getenv("HOST") or DEFAULT_HOST
    resolved_port = port if port is not None else int(os.getenv("PORT", str(DEFAULT_PORT)))
    return ServeSettings(
        host=resolved_host,
        port=resolved_port,
        data_dir=Path(data_dir).expanduser().resolve() if data_dir else None,
        concurrency=concurrency,
    )


def lan_addresses() -> list[str]:
    """本机可用于局域网访问的 IPv4 地址。

    不依赖外部网络：用一个不实际发包的 UDP connect 让内核挑出出口网卡，
    比遍历网卡更省事，也不会因为没装依赖而失败。
    """
    addresses: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        addresses.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidate = info[4][0]
            if candidate not in addresses and not candidate.startswith("127."):
                addresses.append(candidate)
    except OSError:
        pass

    return addresses


def ensure_port_free(host: str, port: int) -> None:
    """提前占一下端口。启动后才发现端口冲突，用户只会看到一个 uvicorn 堆栈。"""
    probe_host = DEFAULT_HOST if host == LAN_HOST else host
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((probe_host, port))
    except OSError as exc:
        raise PortInUse(host, port) from exc
    finally:
        sock.close()


def preflight_warnings(ui_dist: Path | None = None) -> list[str]:
    """返回"能启动但看着不对"的提醒。

    只提醒不拦截——前端没构建时接口仍然可用（可以先跑通链路再补界面），
    硬拦反而把可用的路径堵死。

    注意这里**不含**密钥检查：缺密钥不是"警告"，是启动不了，见 `missing_api_key`。
    """
    warnings: list[str] = []

    if ui_dist is not None and not (ui_dist / "index.html").is_file():
        warnings.append(
            "前端还没有构建，浏览器打开只会看到 404。先在 web/ 目录执行：\n"
            "      npm install && npm run build"
        )

    return warnings


def missing_api_key(provider: str | None = None) -> bool:
    """当前 provider 是否缺密钥。

    实测过一次教训：缺密钥时服务其实是**起不来**的（客户端构造即抛错），
    但旧文案写的是"上传简历后会处理失败"，于是用户先看到"已启动"、
    紧接着一个堆栈——两句话自相矛盾，人只会以为程序坏了。
    所以在启动前就问清楚，把话一次说对。

    这里刻意**不在本模块写死变量名**：密钥叫什么、要不要密钥都由 provider 决定
    （qwen 要 `DASHSCOPE_API_KEY`、deepseek 要 `LLM_API_KEY`、fake 不要）。
    曾经这里写死 `DASHSCOPE_API_KEY`，接入第二个厂商时就会对着配好的用户
    误报"缺密钥"。
    """
    from resume_screening.adapters.llm.factory import has_api_key

    return not has_api_key(provider)


def missing_key_help(provider: str | None = None) -> str:
    """缺密钥时给用户看的出路，按当前 provider 生成。"""
    from resume_screening.adapters.llm.factory import env_names_for, resolve_provider

    resolved = resolve_provider(provider)
    names = env_names_for(resolved)
    if not names:
        # 未知 provider：真正的报错交给 build_llm_client，它会列出合法取值
        return (
            f"未知的模型 provider：{resolved}。请检查 .env 中的 LLM_PROVIDER，"
            "或在启动时用 LLM_PROVIDER=fake 先看界面"
        )
    name = names[0]
    return (
        f"缺少 {name}，服务无法处理简历。二选一：\n"
        f"      1) 在项目根目录建 .env（可复制 .env.example），填入 {name}=你的密钥\n"
        "      2) 只想先看看界面：启动时加上 LLM_PROVIDER=fake（离线，不调用模型）"
    )


#: 兼容旧引用：默认 provider（qwen）下的提示文案
MISSING_KEY_HELP = missing_key_help("qwen")


def serve(
    host: str | None = typer.Option(None, "--host", help="监听地址，默认 127.0.0.1（仅本机）"),
    port: int | None = typer.Option(None, "--port", "-p", help="监听端口，默认 8000"),
    data_dir: str | None = typer.Option(
        None, "--data-dir", help="数据目录（数据库与上传的简历），默认 data/ 下"
    ),
    concurrency: int | None = typer.Option(
        None, "--concurrency", "-c", help="同时处理的简历份数，默认 5"
    ),
    lan: bool = typer.Option(
        False, "--lan", help="允许同一局域网的其他电脑访问，并打印可复制的链接"
    ),
    do_open: bool = typer.Option(
        False, "--open", help="启动后自动用默认浏览器打开工作台"
    ),
) -> None:  # pragma: no cover - 启动入口，纯函数部分已单测
    """启动网页工作台。启动后在浏览器打开提示的地址即可。"""
    from resume_screening.web.app import UI_DIST, create_app, run_server

    settings = resolve_settings(
        host=host, port=port, data_dir=data_dir, concurrency=concurrency, lan=lan
    )

    try:
        ensure_port_free(settings.host, settings.port)
    except PortInUse as exc:
        typer.secho(f"✗ {exc}", fg=typer.colors.RED, err=True)
        typer.echo(
            f"  换一个端口重试，例如：resume-screen serve --port {settings.port + 1}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    # 启动前把"起不来"的原因说清楚，别等 uvicorn 把堆栈甩出来
    if missing_api_key():
        typer.secho(f"✗ {missing_key_help()}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    for warning in preflight_warnings(ui_dist=UI_DIST):
        typer.secho(f"! {warning}", fg=typer.colors.YELLOW, err=True)

    # 先装配再报"已启动"：装配失败时不该出现"已启动 + 堆栈"这种自相矛盾的输出
    app = create_app(
        data_dir=settings.data_dir,
        concurrency=settings.concurrency,
    )

    typer.secho("网页工作台已启动", fg=typer.colors.GREEN)
    typer.echo(f"  打开：{settings.url}")
    if settings.host == LAN_HOST:
        for address in lan_addresses():
            typer.echo(f"  同一局域网的其他电脑可以打开：http://{address}:{settings.port}/")
    if settings.data_dir:
        typer.echo(f"  数据目录：{settings.data_dir}")
    typer.echo("  停止：在本窗口按 Ctrl+C")

    if do_open:
        typer.echo("  正在打开浏览器…")
        # 延迟一下再开：uvicorn 真正开始监听之前打开，会吃到"连接被拒"
        threading.Timer(1.0, lambda: webbrowser.open(settings.url)).start()

    run_server(app, host=settings.host, port=settings.port)
