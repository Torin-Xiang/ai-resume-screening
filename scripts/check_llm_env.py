"""诊断：确认 `.env` 真的被加载、模型通道是否已切到真实模型。

用途：换密钥 / 接新厂商 / 出现"明明配了却说缺密钥"时，先跑这个把链路摆出来。
它只读配置、**不发任何请求**，因此不消耗 token。

    .venv/Scripts/python.exe scripts/check_llm_env.py

注意：`load_dotenv()` 内部靠调用方的 `__file__` 向上找 `.env`，
所以本脚本必须以真实文件形式运行；用 `python - <<'PY'`（stdin）执行会
在 `find_dotenv()` 里抛 AssertionError。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 允许直接 `python scripts/check_llm_env.py` 而无需先 pip install -e .
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from resume_screening.adapters.llm.factory import (  # noqa: E402
    build_llm_client,
    env_names_for,
    has_api_key,
    resolve_provider,
)

SECRET_NAMES = {"LLM_API_KEY", "DASHSCOPE_API_KEY"}


def mask(value: str | None) -> str:
    """密钥只露前后几位。诊断输出经常要被贴进聊天窗口排错。"""
    if not value:
        return "（未读到）"
    return f"{value[:7]}…{value[-4:]}（共 {len(value)} 字符）"


def main() -> int:
    print("—" * 58)
    print("① 环境变量读取结果（来自 .env 或 shell）")
    for name in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "DASHSCOPE_API_KEY"):
        raw = os.getenv(name)
        print(f"   {name:<20}{mask(raw) if name in SECRET_NAMES else (raw or '（未读到）')}")

    print("—" * 58)
    print("② provider 解析结果")
    provider = resolve_provider()
    print(f"   resolve_provider()    {provider}")
    print(f"   认可的密钥变量        {env_names_for(provider) or '（不需要密钥）'}")
    print(f"   has_api_key()         {has_api_key(provider)}")

    print("—" * 58)
    print("③ 装配出的客户端")
    try:
        client = build_llm_client()
    except Exception as exc:  # noqa: BLE001 - 诊断脚本要把原始报错原样呈现
        print(f"   装配失败：{type(exc).__name__}: {exc}")
        return 1

    print(f"   类型                  {type(client).__name__}")
    print(f"   name                  {client.name}")
    print(f"   model                 {client.model}")
    print(f"   base_url              {client.base_url}")
    fake = client.name == "fake"
    print(f"   假模拟模型模式        {'⚠ 仍开启（不会产出真实结果）' if fake else '✓ 已关闭'}")
    print("—" * 58)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
