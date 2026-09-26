"""M7：前端工作台与后端的接口契约。

为什么单独一个文件——M7 是项目里第一次出现"前后端两套代码各自演进"。
这类结构最典型的故障不是逻辑写错，而是**接口漂移**：
前端改了一个路径（或后端重命名了一个路由），TypeScript 编译照样通过、
后端单测照样全绿，直到人打开浏览器点一下才发现 404。
所以这里把"前端声明的每一个接口，后端都必须存在"变成一条会失败的断言。

另一条是构建产物完整性：`index.html` 里引用的资源必须真的存在、真的能被
正确 MIME 下发。dist 是构建产物（不入库），曾经出现过改了源码没重新构建、
浏览器加载到旧包的幽灵问题，这条断言让那种情况立刻暴露。

本文件不联网、不需要 API key，也不启动真实模型。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resume_screening.web.app import UI_DIST, create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_SRC = REPO_ROOT / "web" / "src"


class _NullLLM:
    """契约测试不触发任何模型调用，给个占位即可。"""

    name = "null"


@pytest.fixture()
def app(tmp_path: Path):
    return create_app(data_dir=tmp_path / "appdata", llm=_NullLLM(), parser=object())


def backend_paths(app) -> set[tuple[str, str]]:
    """从 OpenAPI schema 取 (方法, 归一化路径) 全集。

    用 schema 而不是遍历 `app.routes`：路由是按 router 分组的，扁平遍历在
    不同 FastAPI 版本下形态不一致，schema 是稳定契约（也确实就是对外契约本身）。
    """
    schema = app.openapi()
    return {
        (method.upper(), normalize(path))
        for path, operations in schema["paths"].items()
        for method in operations
    }


def normalize(path: str) -> str:
    """把路径参数统一成 `*`，这样前后端各自的参数命名差异不影响比对。"""
    return re.sub(r"\{[^}]*\}", "*", path.split("?", 1)[0]).rstrip("/") or "/"


# 匹配 `/api/...` 字面量（单引号、双引号、模板字符串都算），允许内含 ${...}
FRONTEND_API_LITERAL = re.compile(r"""[`'"](/api/[^`'"]*)[`'"]""")


def frontend_calls() -> dict[str, set[str]]:
    """扫前端源码，收集所有 `/api/...` 调用点，返回 {归一化路径: {来源文件}}。"""
    calls: dict[str, set[str]] = {}
    for source in sorted(WEB_SRC.rglob("*")):
        if source.suffix not in {".ts", ".tsx"}:
            continue
        text = source.read_text(encoding="utf-8")
        for match in FRONTEND_API_LITERAL.finditer(text):
            path = normalize(re.sub(r"\$\{[^}]*\}", "*", match.group(1)))
            calls.setdefault(path, set()).add(source.relative_to(REPO_ROOT).as_posix())
    return calls


# ——————————————————— 接口契约 ———————————————————

# 前端**调用了但后端故意不存在**的例外。写在这里等于留一份书面理由，
# 不允许悄悄加一个"反正没人发现"的路由缺口。
KNOWN_MISSING: dict[str, str] = {}


def test_frontend_scan_finds_calls() -> None:
    """先保证扫描器本身有效——扫不到东西的契约测试会假装通过。"""
    calls = frontend_calls()
    assert len(calls) >= 10, f"只扫到 {len(calls)} 个接口，扫描逻辑可能失效：{sorted(calls)}"
    assert "/api/jobs/*/events" in calls, "SSE 端点应被扫到"


def test_every_frontend_call_exists_in_backend(app) -> None:
    """前端声明的每个接口，后端必须存在，否则运行时才 404。"""
    known = {path for _, path in backend_paths(app)}
    missing = {
        path: sources
        for path, sources in frontend_calls().items()
        if path not in known and path not in KNOWN_MISSING
    }
    assert not missing, "前端调用了后端不存在的接口：\n" + "\n".join(
        f"  {path}  ← {', '.join(sorted(sources))}" for path, sources in sorted(missing.items())
    )


def test_frontend_covers_core_journey(app) -> None:
    """产品方案 §5 的 P0 主链路：建岗 → 设标准 → 上传 → 排名 → 复核。

    逐条钉住这几步对应的端点，防止"重构时顺手删了一个"而没人发现。
    """
    known = {path for _, path in backend_paths(app)}
    for path in (
        "/api/jobs",
        "/api/jobs/*/rubric",
        "/api/jobs/*/rubric/generate",
        "/api/jobs/*/resumes",
        "/api/jobs/*/ranking",
        "/api/jobs/*/review",
        "/api/jobs/*/events",
    ):
        assert path in known, f"主链路端点缺失：{path}"


# 后端已实现、前端必须真的挂上的 P0 动作。
# 上一条断言管的是"前端调了后端没有"，这一条管反方向：**后端做了但界面没接**——
# 那是交付里最难发现的一类缺口，功能"存在"但用户碰不到，测试全绿也说明不了什么。
WIRED_P0_ENDPOINTS: dict[str, str] = {
    "/api/jobs/*/rubric/propose": "对话式改标准：提交一句话",
    "/api/jobs/*/rubric/apply": "对话式改标准：确认生效 / 部分采纳",
    "/api/jobs/*/rubric/rollback": "标准版本回滚",
    "/api/jobs/*/rubric/versions": "历史版本列表",
    "/api/jobs/*/resumes/text": "扫描件兜底：粘贴简历文本",
    "/api/jobs/*/export": "导出 Excel",
}


def test_every_p0_action_is_wired_into_the_ui() -> None:
    """每个 P0 动作都要在界面上有入口，不能只躺在后端。"""
    calls = frontend_calls()
    unwired = [
        f"  {path}  （{label}）"
        for path, label in WIRED_P0_ENDPOINTS.items()
        if path not in calls
    ]
    assert not unwired, "以下 P0 动作后端已实现，但前端没有任何调用点：\n" + "\n".join(unwired)


# ——————————————————— 构建产物完整性 ———————————————————

ASSET_REFERENCE = re.compile(r'(?:src|href)="(/[^"]+)"')


def dist_assets() -> list[str]:
    index = UI_DIST / "index.html"
    if not index.is_file():
        pytest.skip("前端尚未构建（web/dist/index.html 不存在），跳过产物完整性检查")
    return ASSET_REFERENCE.findall(index.read_text(encoding="utf-8"))


def test_index_html_references_at_least_one_script() -> None:
    """构建产物必须带脚本入口；否则说明构建是残的，页面会白屏。"""
    assets = dist_assets()
    assert any(path.endswith(".js") for path in assets), f"index.html 未引用任何 JS：{assets}"


def test_all_referenced_assets_are_served(app) -> None:
    """`index.html` 引用的资源必须都在 dist 里、且能被后端以正确 MIME 取到。

    这条同时覆盖两个已踩过的坑：dist 与源码不同步（引用了不存在的 hash 文件）、
    以及 CSS 被当成 text/plain 下发。
    """
    assets = dist_assets()
    local = [path for path in assets if path.startswith("/assets/")]
    assert local, f"index.html 未引用 /assets/ 下的资源：{assets}"

    with TestClient(app) as client:
        for path in local:
            assert (UI_DIST / path.lstrip("/")).is_file(), f"{path} 在 dist 中不存在（构建产物不完整）"
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} 返回 {resp.status_code}"
            content_type = resp.headers["content-type"]
            if path.endswith(".js"):
                assert "javascript" in content_type, f"{path} 的 Content-Type 是 {content_type}"
            elif path.endswith(".css"):
                assert "text/css" in content_type, f"{path} 的 Content-Type 是 {content_type}"
