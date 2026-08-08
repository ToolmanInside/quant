from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.config import settings
from backend.data.providers import TushareDataProvider
from backend.models import (
    PaperAdvanceRequest,
    PaperSimulationRequest,
)
from backend.paper_store import PaperStore, account_state_path
from backend.paper_trading import (
    advance_paper_simulation,
    replay_paper_simulation,
)


logger = logging.getLogger("uvicorn.error")


app = FastAPI(
    title=f"{settings.app_name} API",
    version=settings.app_version,
    description="Windows本地量化研究与回测服务。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def validation_message(exc: RequestValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        message = str(error.get("msg", "参数不合法"))
        message = message.removeprefix("Value error, ")
        messages.append(f"{location}：{message}" if location else message)
    return "；".join(messages) or "请求参数不合法"


@app.exception_handler(RequestValidationError)
async def request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    message = validation_message(exc)
    logger.error(
        "Request validation failed: %s %s - %s",
        request.method,
        request.url.path,
        message,
    )
    return JSONResponse(status_code=422, content={"detail": message})


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled server error: %s %s",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "服务发生未处理错误，详细信息已输出到命令行。"},
    )


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": settings.app_version}


@app.get("/api/system/status")
def system_status() -> dict:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "mode": "模拟交易",
        "tushare_configured": bool(settings.tushare_token),
        "data_provider": "Tushare Pro",
        "broker_connected": False,
    }


@app.get("/api/paper/dashboard")
def paper_dashboard(account_id: str = "default") -> dict:
    store = PaperStore(account_state_path(account_id))
    try:
        return store.dashboard(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()


@app.post("/api/paper/replay")
def paper_replay(request: PaperSimulationRequest) -> dict:
    if not settings.tushare_token:
        raise HTTPException(status_code=503, detail="尚未配置 TUSHARE_TOKEN。")
    logger.info("Paper replay requested for account %s", request.account_id)
    store = PaperStore(account_state_path(request.account_id))
    try:
        return replay_paper_simulation(
            request,
            TushareDataProvider(settings.tushare_token),
            store,
        )
    except ValueError as exc:
        logger.warning("Paper replay rejected: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Paper replay failed")
        raise HTTPException(
            status_code=502,
            detail=f"模拟交易回放失败：{exc}",
        ) from exc
    finally:
        store.close()


@app.post("/api/paper/advance")
def paper_advance(request: PaperAdvanceRequest) -> dict:
    if not settings.tushare_token:
        raise HTTPException(status_code=503, detail="尚未配置 TUSHARE_TOKEN。")
    logger.info("Paper daily advance requested for account %s", request.account_id)
    store = PaperStore(account_state_path(request.account_id))
    try:
        return advance_paper_simulation(
            request,
            TushareDataProvider(settings.tushare_token),
            store,
        )
    except ValueError as exc:
        logger.warning("Paper daily advance rejected: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Paper daily advance failed")
        raise HTTPException(
            status_code=502,
            detail=f"每日模拟运行失败：{exc}",
        ) from exc
    finally:
        store.close()
