"""
AKShare Gateway — 将 akshare 封装为 HTTP API 微服务

部署在独立 Docker 容器中，供 ai-stock 主服务通过内网调用。
解决云服务器直接调用 akshare 时被东方财富反爬封锁的问题。

所有接口返回 JSON 格式的 DataFrame（orient="records"）。
"""

import time
import traceback
from datetime import datetime
from typing import Optional

import akshare as ak
import pandas as pd
import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger()

app = FastAPI(
    title="AKShare Gateway",
    description="AKShare HTTP API 网关 — 为 ai-stock 提供数据代理",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================================================================
#  工具函数
# =====================================================================

def _df_to_response(df: Optional[pd.DataFrame], name: str = "data") -> JSONResponse:
    """将 DataFrame 转为 JSON 响应"""
    if df is None or df.empty:
        return JSONResponse(
            content={"status": "ok", "count": 0, "data": []},
        )
    # 处理 NaN / Inf → null
    df = df.where(pd.notnull(df), None)
    records = df.to_dict(orient="records")
    return JSONResponse(
        content={"status": "ok", "count": len(records), "data": records},
    )


def _retry(func, *args, max_retries: int = 3, delay: float = 2.0, **kwargs):
    """带重试的函数调用"""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            result = func(*args, **kwargs)
            return result
        except Exception as e:
            last_err = e
            logger.warning(
                "akshare call failed, retrying",
                func=func.__name__,
                attempt=attempt,
                error=str(e),
            )
            if attempt < max_retries:
                time.sleep(delay * attempt)
    raise last_err


# =====================================================================
#  健康检查
# =====================================================================

@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok", "service": "akshare-gateway", "time": datetime.now().isoformat()}


# =====================================================================
#  A 股实时行情
# =====================================================================

@app.get("/api/stock/zh_a_spot_em")
async def stock_zh_a_spot_em():
    """
    获取沪深京 A 股实时行情（东方财富）

    对应 akshare: ak.stock_zh_a_spot_em()
    """
    try:
        logger.info("Fetching stock_zh_a_spot_em")
        df = _retry(ak.stock_zh_a_spot_em)
        logger.info("stock_zh_a_spot_em success", count=len(df) if df is not None else 0)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_zh_a_spot_em failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  个股历史K线
# =====================================================================

@app.get("/api/stock/zh_a_hist")
async def stock_zh_a_hist(
    symbol: str = Query(..., description="股票代码，如 600519"),
    period: str = Query("daily", description="周期: daily/weekly/monthly"),
    start_date: str = Query("", description="开始日期 YYYYMMDD"),
    end_date: str = Query("", description="结束日期 YYYYMMDD"),
    adjust: str = Query("qfq", description="复权: qfq/hfq/空字符串"),
):
    """
    获取个股历史K线数据

    对应 akshare: ak.stock_zh_a_hist(symbol, period, start_date, end_date, adjust)
    """
    try:
        logger.info("Fetching stock_zh_a_hist", symbol=symbol, period=period)
        kwargs = {"symbol": symbol, "period": period, "adjust": adjust}
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        df = _retry(ak.stock_zh_a_hist, **kwargs)
        logger.info("stock_zh_a_hist success", symbol=symbol, count=len(df) if df is not None else 0)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_zh_a_hist failed", symbol=symbol, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  涨停池 / 跌停池
# =====================================================================

@app.get("/api/stock/zt_pool_em")
async def stock_zt_pool_em(
    date: str = Query(..., description="日期 YYYYMMDD"),
):
    """
    获取涨停股池

    对应 akshare: ak.stock_zt_pool_em(date)
    """
    try:
        logger.info("Fetching stock_zt_pool_em", date=date)
        df = _retry(ak.stock_zt_pool_em, date=date)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_zt_pool_em failed", date=date, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/zt_pool_dtgc_em")
async def stock_zt_pool_dtgc_em(
    date: str = Query(..., description="日期 YYYYMMDD"),
):
    """
    获取跌停股池

    对应 akshare: ak.stock_zt_pool_dtgc_em(date)
    """
    try:
        logger.info("Fetching stock_zt_pool_dtgc_em", date=date)
        df = _retry(ak.stock_zt_pool_dtgc_em, date=date)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_zt_pool_dtgc_em failed", date=date, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  概念板块
# =====================================================================

@app.get("/api/stock/board_concept_name_em")
async def stock_board_concept_name_em():
    """
    获取东方财富概念板块列表

    对应 akshare: ak.stock_board_concept_name_em()
    """
    try:
        logger.info("Fetching stock_board_concept_name_em")
        df = _retry(ak.stock_board_concept_name_em)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_board_concept_name_em failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/board_concept_cons_em")
async def stock_board_concept_cons_em(
    symbol: str = Query(..., description="概念板块名称"),
):
    """
    获取东方财富概念板块成份股

    对应 akshare: ak.stock_board_concept_cons_em(symbol)
    """
    try:
        logger.info("Fetching stock_board_concept_cons_em", symbol=symbol)
        df = _retry(ak.stock_board_concept_cons_em, symbol=symbol)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_board_concept_cons_em failed", symbol=symbol, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/board_concept_name_ths")
async def stock_board_concept_name_ths():
    """
    获取同花顺概念板块列表

    对应 akshare: ak.stock_board_concept_name_ths()
    """
    try:
        logger.info("Fetching stock_board_concept_name_ths")
        df = _retry(ak.stock_board_concept_name_ths)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_board_concept_name_ths failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/board_concept_cons_ths")
async def stock_board_concept_cons_ths(
    symbol: str = Query(..., description="概念板块名称"),
):
    """
    获取同花顺概念板块成份股

    对应 akshare: ak.stock_board_concept_cons_ths(symbol)
    """
    try:
        logger.info("Fetching stock_board_concept_cons_ths", symbol=symbol)
        df = _retry(ak.stock_board_concept_cons_ths, symbol=symbol)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_board_concept_cons_ths failed", symbol=symbol, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  全球财经快讯
# =====================================================================

@app.get("/api/stock/info_global_em")
async def stock_info_global_em():
    """
    全球财经快讯（东方财富）

    对应 akshare: ak.stock_info_global_em()
    """
    try:
        logger.info("Fetching stock_info_global_em")
        df = _retry(ak.stock_info_global_em)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_info_global_em failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/info_global_sina")
async def stock_info_global_sina():
    """
    全球财经快讯（新浪）

    对应 akshare: ak.stock_info_global_sina()
    """
    try:
        logger.info("Fetching stock_info_global_sina")
        df = _retry(ak.stock_info_global_sina)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_info_global_sina failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/info_global_cls")
async def stock_info_global_cls():
    """
    全球财经快讯（财联社）

    对应 akshare: ak.stock_info_global_cls()
    """
    try:
        logger.info("Fetching stock_info_global_cls")
        df = _retry(ak.stock_info_global_cls)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_info_global_cls failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  股票代码名称
# =====================================================================

@app.get("/api/stock/info_a_code_name")
async def stock_info_a_code_name():
    """
    获取A股全部股票代码和名称

    对应 akshare: ak.stock_info_a_code_name()
    """
    try:
        logger.info("Fetching stock_info_a_code_name")
        df = _retry(ak.stock_info_a_code_name)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_info_a_code_name failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  股东信息
# =====================================================================

@app.get("/api/stock/gdfx_top_10_em")
async def stock_gdfx_top_10_em(
    symbol: str = Query(..., description="股票代码（含市场前缀，如 sh600519）"),
    date: str = Query(..., description="报告期 YYYYMMDD"),
):
    """
    获取个股十大股东

    对应 akshare: ak.stock_gdfx_top_10_em(symbol, date)
    """
    try:
        logger.info("Fetching stock_gdfx_top_10_em", symbol=symbol, date=date)
        df = _retry(ak.stock_gdfx_top_10_em, symbol=symbol, date=date)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_gdfx_top_10_em failed", symbol=symbol, date=date, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  IPO 数据
# =====================================================================

@app.get("/api/stock/ipo_declare_em")
async def stock_ipo_declare_em():
    """
    获取A股IPO申报信息

    对应 akshare: ak.stock_ipo_declare_em()
    """
    try:
        logger.info("Fetching stock_ipo_declare_em")
        df = _retry(ak.stock_ipo_declare_em)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_ipo_declare_em failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/hk_ipo_wait_board_em")
async def stock_hk_ipo_wait_board_em():
    """
    获取港股IPO待上市板数据

    对应 akshare: ak.stock_hk_ipo_wait_board_em()
    """
    try:
        logger.info("Fetching stock_hk_ipo_wait_board_em")
        if not hasattr(ak, "stock_hk_ipo_wait_board_em"):
            raise HTTPException(status_code=501, detail="此 akshare 版本不支持 stock_hk_ipo_wait_board_em")
        df = _retry(ak.stock_hk_ipo_wait_board_em)
        return _df_to_response(df)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("stock_hk_ipo_wait_board_em failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/hk_spot_em")
async def stock_hk_spot_em():
    """
    获取港股实时行情

    对应 akshare: ak.stock_hk_spot_em()
    """
    try:
        logger.info("Fetching stock_hk_spot_em")
        df = _retry(ak.stock_hk_spot_em)
        return _df_to_response(df)
    except Exception as e:
        logger.error("stock_hk_spot_em failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  通用代理接口（兜底）
# =====================================================================

@app.get("/api/akshare/{func_name}")
async def generic_akshare_proxy(func_name: str):
    """
    通用 akshare 函数代理（仅限无参数的函数）

    如果需要调用其他未单独封装的 akshare 函数，可通过此接口。
    例如: GET /api/akshare/stock_zh_index_daily_em
    """
    try:
        func = getattr(ak, func_name, None)
        if func is None:
            raise HTTPException(status_code=404, detail=f"akshare 无此函数: {func_name}")
        if not callable(func):
            raise HTTPException(status_code=400, detail=f"{func_name} 不可调用")

        logger.info("Generic akshare proxy call", func=func_name)
        df = _retry(func)
        return _df_to_response(df)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Generic akshare proxy failed", func=func_name, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  启动
# =====================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9898, log_level="info")
