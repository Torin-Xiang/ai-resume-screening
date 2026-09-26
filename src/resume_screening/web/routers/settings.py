"""模型通道设置：查看当前通道、运行时切换（界面"切换模型"）。

切换**立即生效**（之后的简历用新通道评），并且持久化到
`data_dir/llm_config.json`，重启后保持——否则每次重启都悄悄退回
.env 默认，界面显示的和实际用的对不上。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ...services import llm_settings
from ...services.worker import ScreeningWorker
from ..deps import get_worker
from ..schemas import LlmSettingsIn, LlmSettingsOut

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/llm", response_model=LlmSettingsOut)
def get_llm_settings(worker: ScreeningWorker = Depends(get_worker)) -> LlmSettingsOut:
    client = worker.llm
    return LlmSettingsOut(
        provider=getattr(client, "name", "unknown"),
        model=getattr(client, "model", ""),
        **llm_settings.snapshot(),
    )


@router.post("/llm", response_model=LlmSettingsOut)
def set_llm_settings(
    payload: LlmSettingsIn,
    request: Request,
    worker: ScreeningWorker = Depends(get_worker),
) -> LlmSettingsOut:
    choice, error = llm_settings.validate(payload.provider, payload.model)
    if error is not None:
        raise HTTPException(status_code=400, detail=error)

    try:
        client = llm_settings.build_client(choice)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 缺密钥等装配失败都算"切不过去"
        raise HTTPException(status_code=400, detail=f"切换失败：{exc}") from exc

    worker.set_llm(client)
    request.app.state.llm = client
    llm_settings.save_choice(
        request.app.state.db.data_dir, provider=choice["provider"], model=choice["model"]
    )
    return LlmSettingsOut(
        provider=choice["provider"],
        model=choice["model"],
        **llm_settings.snapshot(),
    )
