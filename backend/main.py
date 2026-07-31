from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.backtest import run_moving_average_backtest
from backend.config import settings
from backend.data.providers import TushareDataProvider
from backend.matrix import run_strategy_matrix
from backend.models import (
    BacktestRequest,
    PaperAdvanceRequest,
    PaperSimulationRequest,
    StrategyMatrixRequest,
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
        "mode": "回测 + 模拟交易",
        "tushare_configured": bool(settings.tushare_token),
        "data_provider": "Tushare Pro",
        "broker_connected": False,
    }


@app.get("/api/strategies")
def strategies() -> list[dict]:
    return [
        {
            "id": "moving_average",
            "name": "双均线趋势",
            "status": "available",
            "description": "短均线上穿长均线后持有，反向时空仓。",
        },
        {
            "id": "momentum",
            "name": "价格动量",
            "status": "available",
            "description": "选择中短期累计涨幅领先且趋势为正的标的。",
        },
        {
            "id": "breakout",
            "name": "通道突破",
            "status": "available",
            "description": "突破前期价格通道买入，跌破退出通道平仓。",
        },
    ]


@app.post("/api/backtest")
def backtest(request: BacktestRequest) -> dict:
    if not settings.tushare_token:
        logger.error("Backtest rejected: TUSHARE_TOKEN is not configured.")
        raise HTTPException(
            status_code=503,
            detail="尚未配置 TUSHARE_TOKEN，请先复制 .env.example 为 .env 并填写 Token。",
        )

    provider = TushareDataProvider(settings.tushare_token)

    try:
        market_data = provider.fetch_daily(
            request.symbol,
            request.start_date,
            request.end_date,
        )
        result = run_moving_average_backtest(
            market_data.frame,
            request,
            market_data.source,
        )
        return {"symbol": request.symbol, **result}
    except ValueError as exc:
        logger.warning("Backtest rejected for %s: %s", request.symbol, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Backtest failed for %s", request.symbol)
        raise HTTPException(
            status_code=502,
            detail=f"数据获取或回测失败：{exc}",
        ) from exc


@app.post("/api/research/strategy-matrix")
def strategy_matrix(request: StrategyMatrixRequest) -> dict:
    if not settings.tushare_token:
        logger.error("Strategy matrix rejected: TUSHARE_TOKEN is not configured.")
        raise HTTPException(status_code=503, detail="尚未配置 TUSHARE_TOKEN。")

    provider = TushareDataProvider(settings.tushare_token)
    logger.info(
        "Strategy matrix started: %s symbols, %s to %s",
        len(request.symbols),
        request.start_date,
        request.end_date,
    )
    try:
        result = run_strategy_matrix(
            request,
            provider,
            progress=lambda message: logger.info("Strategy matrix: %s", message),
        )
        logger.info(
            "Strategy matrix completed. Best: %s",
            (
                f"{result['best']['strategy_name']} × "
                f"{result['best']['frequency_name']} "
                f"({result['best']['score']})"
                if result["best"]
                else "none"
            ),
        )
        return result
    except ValueError as exc:
        logger.warning("Strategy matrix rejected: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Strategy matrix failed")
        raise HTTPException(
            status_code=502,
            detail=f"策略矩阵执行失败：{exc}",
        ) from exc


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
