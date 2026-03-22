"""
AKShare Gateway — 统一第三方数据接口网关平台

部署在独立服务器/Docker 中，供 ai-stock 主服务通过内网调用。
解决云服务器直接调用 akshare 时被东方财富反爬封锁的问题。

功能:
  1. 已注册接口 — 参数校验 + 定制化处理
  2. 通用代理 — 任意 akshare 函数代理（白名单保护）
  3. TTL 缓存 — 减少对东财的请求频率
  4. 请求限流 — 自动随机延迟，防止触发风控
  5. 统计监控 — 请求次数、缓存命中率、接口耗时

所有接口返回 JSON 格式: {"status":"ok","count":N,"data":[...]}
"""

import hashlib
import random
import time
import traceback
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Optional

import akshare as ak
import pandas as pd
import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger()


# =====================================================================
#  TTL 缓存
# =====================================================================

class TTLCache:
    """线程安全的 TTL 内存缓存"""

    def __init__(self):
        self._store: Dict[str, tuple] = {}  # key -> (data, expire_time)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key in self._store:
                data, expire_at = self._store[key]
                if time.time() < expire_at:
                    self._hits += 1
                    return data
                else:
                    del self._store[key]
            self._misses += 1
            return None

    def set(self, key: str, data: Any, ttl: float):
        with self._lock:
            self._store[key] = (data, time.time() + ttl)

    def clear(self):
        with self._lock:
            self._store.clear()

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "size": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{self._hits / total * 100:.1f}%" if total > 0 else "N/A",
        }


cache = TTLCache()

# 缓存 TTL 配置（秒）
CACHE_TTL = {
    "default": 60,
    # 全市场行情 — 1分钟（高频变化）
    "stock_zh_a_spot_em": 60,
    "stock_hk_spot_em": 60,
    # 个股历史K线 — 5分钟
    "stock_zh_a_hist": 300,
    # 个股指标 — 5分钟（PE/PB分位不常变）
    "stock_a_indicator_lg": 300,
    "stock_individual_info_em": 300,
    # 板块列表 — 10分钟
    "board_concept_name_em": 600,
    "board_concept_name_ths": 600,
    # 涨停/跌停池 — 2分钟
    "stock_zt_pool_em": 120,
    "stock_zt_pool_dtgc_em": 120,
    # 北向资金 — 3分钟
    "stock_hsgt_hold_stock_em": 180,
    "stock_individual_fund_flow_rank": 180,
    # 新闻快讯 — 2分钟
    "stock_info_global_em": 120,
    "stock_info_global_cls": 120,
    # 代码名称列表 — 1小时（极少变化）
    "stock_info_a_code_name": 3600,
}


def _cache_key(func_name: str, **kwargs) -> str:
    """生成缓存 key"""
    if not kwargs:
        return func_name
    sorted_params = sorted(kwargs.items())
    param_str = "&".join(f"{k}={v}" for k, v in sorted_params)
    param_hash = hashlib.md5(param_str.encode()).hexdigest()[:8]
    return f"{func_name}:{param_hash}"


def _get_ttl(func_name: str) -> float:
    """获取函数的缓存 TTL"""
    # 精确匹配
    if func_name in CACHE_TTL:
        return CACHE_TTL[func_name]
    # 去掉 stock_ 前缀匹配
    short = func_name.replace("stock_", "")
    if short in CACHE_TTL:
        return CACHE_TTL[short]
    return CACHE_TTL["default"]


# =====================================================================
#  请求限流 & 统计
# =====================================================================

_last_request_time = 0.0
_rate_lock = threading.Lock()

# 最小请求间隔（秒）— 防止频率过高触发东财风控
MIN_REQUEST_INTERVAL = 0.3


def _rate_limit():
    """请求间自动随机延迟"""
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            delay = MIN_REQUEST_INTERVAL - elapsed + random.uniform(0.1, 0.5)
            time.sleep(delay)
        _last_request_time = time.time()


# 请求统计
_stats_lock = threading.Lock()
_request_stats: Dict[str, dict] = defaultdict(lambda: {
    "count": 0, "errors": 0, "total_ms": 0, "cache_hits": 0
})


def _record_stat(func_name: str, elapsed_ms: float, is_error: bool = False, cache_hit: bool = False):
    """记录请求统计"""
    with _stats_lock:
        s = _request_stats[func_name]
        s["count"] += 1
        s["total_ms"] += elapsed_ms
        if is_error:
            s["errors"] += 1
        if cache_hit:
            s["cache_hits"] += 1


# =====================================================================
#  FastAPI 应用
# =====================================================================

app = FastAPI(
    title="AKShare Gateway",
    description="统一第三方数据接口网关 — 为 ai-stock 提供数据代理，支持缓存、限流、监控",
    version="2.0.0",
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


def _cached_call(func_name: str, func, *args, **kwargs) -> pd.DataFrame:
    """
    带缓存的 akshare 调用

    1. 检查缓存
    2. 未命中则限流 + 调用 + 缓存结果
    """
    key = _cache_key(func_name, **kwargs)
    cached = cache.get(key)
    if cached is not None:
        logger.debug("Cache hit", func=func_name, key=key)
        return cached

    # 限流
    _rate_limit()

    # 调用
    df = _retry(func, *args, **kwargs)

    # 缓存
    ttl = _get_ttl(func_name)
    if df is not None and not df.empty:
        cache.set(key, df, ttl)
        logger.debug("Cached result", func=func_name, key=key, ttl=ttl, rows=len(df))

    return df


# =====================================================================
#  健康检查 & 监控
# =====================================================================

@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "ok",
        "service": "akshare-gateway",
        "version": "2.0.0",
        "time": datetime.now().isoformat(),
        "cache": cache.stats,
    }


@app.get("/stats")
async def stats():
    """请求统计 & 监控"""
    with _stats_lock:
        endpoints = {}
        for name, s in sorted(_request_stats.items()):
            avg_ms = s["total_ms"] / s["count"] if s["count"] > 0 else 0
            endpoints[name] = {
                "requests": s["count"],
                "errors": s["errors"],
                "cache_hits": s["cache_hits"],
                "avg_ms": round(avg_ms, 1),
                "error_rate": f"{s['errors'] / s['count'] * 100:.1f}%" if s["count"] > 0 else "0%",
            }

    return {
        "status": "ok",
        "uptime": datetime.now().isoformat(),
        "cache": cache.stats,
        "endpoints": endpoints,
    }


@app.post("/cache/clear")
async def clear_cache():
    """清除所有缓存"""
    cache.clear()
    logger.info("Cache cleared manually")
    return {"status": "ok", "message": "Cache cleared"}


# =====================================================================
#  A 股实时行情
# =====================================================================

@app.get("/api/stock/zh_a_spot_em")
async def stock_zh_a_spot_em():
    """
    获取沪深京 A 股实时行情（东方财富）

    对应 akshare: ak.stock_zh_a_spot_em()
    """
    start = time.time()
    func_name = "stock_zh_a_spot_em"
    try:
        key = _cache_key(func_name)
        cached = cache.get(key)
        if cached is not None:
            _record_stat(func_name, (time.time() - start) * 1000, cache_hit=True)
            return _df_to_response(cached)

        logger.info("Fetching stock_zh_a_spot_em")
        _rate_limit()
        df = _retry(ak.stock_zh_a_spot_em)
        if df is not None and not df.empty:
            cache.set(key, df, _get_ttl(func_name))
        _record_stat(func_name, (time.time() - start) * 1000)
        logger.info("stock_zh_a_spot_em success", count=len(df) if df is not None else 0)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_zh_a_spot_em failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  港股实时行情
# =====================================================================

@app.get("/api/stock/hk_spot_em")
async def stock_hk_spot_em():
    """
    获取港股实时行情

    对应 akshare: ak.stock_hk_spot_em()
    """
    start = time.time()
    func_name = "stock_hk_spot_em"
    try:
        df = _cached_call(func_name, ak.stock_hk_spot_em)
        _record_stat(func_name, (time.time() - start) * 1000)
        logger.info("stock_hk_spot_em success", count=len(df) if df is not None else 0)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_hk_spot_em failed", error=str(e))
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
    start = time.time()
    func_name = "stock_zh_a_hist"
    try:
        kwargs = {"symbol": symbol, "period": period, "adjust": adjust}
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        df = _cached_call(func_name, ak.stock_zh_a_hist, **kwargs)
        _record_stat(func_name, (time.time() - start) * 1000)
        logger.info("stock_zh_a_hist success", symbol=symbol, count=len(df) if df is not None else 0)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_zh_a_hist failed", symbol=symbol, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  个股估值指标（PE/PB历史分位 — 护城河策略核心依赖）
# =====================================================================

@app.get("/api/stock/a_indicator_lg")
async def stock_a_indicator_lg(
    symbol: str = Query(..., description="股票代码，如 600519"),
):
    """
    获取个股 PE/PB/PS 等估值指标历史数据（乐咕乐估）

    对应 akshare: ak.stock_a_indicator_lg(symbol)
    """
    start = time.time()
    func_name = "stock_a_indicator_lg"
    try:
        df = _cached_call(func_name, ak.stock_a_indicator_lg, symbol=symbol)
        _record_stat(func_name, (time.time() - start) * 1000)
        logger.info("stock_a_indicator_lg success", symbol=symbol,
                     count=len(df) if df is not None else 0)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_a_indicator_lg failed", symbol=symbol, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  个股详细信息
# =====================================================================

@app.get("/api/stock/individual_info_em")
async def stock_individual_info_em(
    symbol: str = Query(..., description="股票代码，如 600519"),
):
    """
    获取个股详细信息（总市值、流通市值、行业、上市日期等）

    对应 akshare: ak.stock_individual_info_em(symbol)
    """
    start = time.time()
    func_name = "stock_individual_info_em"
    try:
        df = _cached_call(func_name, ak.stock_individual_info_em, symbol=symbol)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_individual_info_em failed", symbol=symbol, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  北向资金持仓
# =====================================================================

@app.get("/api/stock/hsgt_hold_stock_em")
async def stock_hsgt_hold_stock_em(
    market: str = Query("北向", description="市场: 北向/沪股通/深股通"),
    indicator: str = Query("今日排行", description="排行类型"),
):
    """
    获取北向/沪深通持仓排名

    对应 akshare: ak.stock_hsgt_hold_stock_em(market, indicator)
    """
    start = time.time()
    func_name = "stock_hsgt_hold_stock_em"
    try:
        df = _cached_call(func_name, ak.stock_hsgt_hold_stock_em,
                          market=market, indicator=indicator)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_hsgt_hold_stock_em failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  资金流向排名
# =====================================================================

@app.get("/api/stock/individual_fund_flow_rank")
async def stock_individual_fund_flow_rank(
    indicator: str = Query("今日", description="时间维度: 今日/3日/5日/10日"),
):
    """
    获取个股资金流向排名

    对应 akshare: ak.stock_individual_fund_flow_rank(indicator)
    """
    start = time.time()
    func_name = "stock_individual_fund_flow_rank"
    try:
        df = _cached_call(func_name, ak.stock_individual_fund_flow_rank,
                          indicator=indicator)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_individual_fund_flow_rank failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  涨停池 / 跌停池
# =====================================================================

@app.get("/api/stock/zt_pool_em")
async def stock_zt_pool_em(
    date: str = Query(..., description="日期 YYYYMMDD"),
):
    """获取涨停股池"""
    start = time.time()
    func_name = "stock_zt_pool_em"
    try:
        df = _cached_call(func_name, ak.stock_zt_pool_em, date=date)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_zt_pool_em failed", date=date, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/zt_pool_dtgc_em")
async def stock_zt_pool_dtgc_em(
    date: str = Query(..., description="日期 YYYYMMDD"),
):
    """获取跌停股池"""
    start = time.time()
    func_name = "stock_zt_pool_dtgc_em"
    try:
        df = _cached_call(func_name, ak.stock_zt_pool_dtgc_em, date=date)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_zt_pool_dtgc_em failed", date=date, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  概念板块
# =====================================================================

@app.get("/api/stock/board_concept_name_em")
async def stock_board_concept_name_em():
    """获取东方财富概念板块列表"""
    start = time.time()
    func_name = "stock_board_concept_name_em"
    try:
        df = _cached_call(func_name, ak.stock_board_concept_name_em)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_board_concept_name_em failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/board_concept_cons_em")
async def stock_board_concept_cons_em(
    symbol: str = Query(..., description="概念板块名称"),
):
    """获取东方财富概念板块成份股"""
    start = time.time()
    func_name = "stock_board_concept_cons_em"
    try:
        df = _cached_call(func_name, ak.stock_board_concept_cons_em, symbol=symbol)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_board_concept_cons_em failed", symbol=symbol, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/board_concept_name_ths")
async def stock_board_concept_name_ths():
    """获取同花顺概念板块列表"""
    start = time.time()
    func_name = "stock_board_concept_name_ths"
    try:
        df = _cached_call(func_name, ak.stock_board_concept_name_ths)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_board_concept_name_ths failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/board_concept_cons_ths")
async def stock_board_concept_cons_ths(
    symbol: str = Query(..., description="概念板块名称"),
):
    """获取同花顺概念板块成份股"""
    start = time.time()
    func_name = "stock_board_concept_cons_ths"
    try:
        df = _cached_call(func_name, ak.stock_board_concept_cons_ths, symbol=symbol)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_board_concept_cons_ths failed", symbol=symbol, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  全球财经快讯
# =====================================================================

@app.get("/api/stock/info_global_em")
async def stock_info_global_em():
    """全球财经快讯（东方财富）"""
    start = time.time()
    func_name = "stock_info_global_em"
    try:
        df = _cached_call(func_name, ak.stock_info_global_em)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_info_global_em failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/info_global_sina")
async def stock_info_global_sina():
    """全球财经快讯（新浪）"""
    start = time.time()
    func_name = "stock_info_global_sina"
    try:
        df = _cached_call(func_name, ak.stock_info_global_sina)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_info_global_sina failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/info_global_cls")
async def stock_info_global_cls():
    """全球财经快讯（财联社）"""
    start = time.time()
    func_name = "stock_info_global_cls"
    try:
        df = _cached_call(func_name, ak.stock_info_global_cls)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_info_global_cls failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  股票代码名称
# =====================================================================

@app.get("/api/stock/info_a_code_name")
async def stock_info_a_code_name():
    """获取A股全部代码和名称"""
    start = time.time()
    func_name = "stock_info_a_code_name"
    try:
        df = _cached_call(func_name, ak.stock_info_a_code_name)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_info_a_code_name failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  股东信息
# =====================================================================

@app.get("/api/stock/gdfx_top_10_em")
async def stock_gdfx_top_10_em(
    symbol: str = Query(..., description="股票代码"),
    date: str = Query(..., description="报告期 YYYYMMDD"),
):
    """获取个股十大股东"""
    start = time.time()
    func_name = "stock_gdfx_top_10_em"
    try:
        df = _cached_call(func_name, ak.stock_gdfx_top_10_em, symbol=symbol, date=date)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_gdfx_top_10_em failed", symbol=symbol, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  IPO 数据
# =====================================================================

@app.get("/api/stock/ipo_declare_em")
async def stock_ipo_declare_em():
    """获取A股IPO申报信息"""
    start = time.time()
    func_name = "stock_ipo_declare_em"
    try:
        df = _cached_call(func_name, ak.stock_ipo_declare_em)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("stock_ipo_declare_em failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  通用带参数代理（万能接口）
# =====================================================================

# 白名单前缀 — 只允许调用这些前缀的函数
ALLOWED_PREFIXES = (
    "stock_", "bond_", "fund_", "index_", "macro_",
    "futures_", "option_", "forex_", "crypto_",
)


@app.get("/api/akshare/{func_name}")
async def generic_akshare_proxy(func_name: str, request: Request):
    """
    通用 akshare 函数代理（支持任意参数）

    通过 query params 传递参数:
      GET /api/akshare/stock_a_indicator_lg?symbol=600519
      GET /api/akshare/stock_zh_a_hist?symbol=600519&period=daily&start_date=20240101

    安全限制: 只允许调用白名单前缀的函数
    """
    start = time.time()

    # 安全检查
    if not func_name.startswith(ALLOWED_PREFIXES):
        raise HTTPException(
            status_code=403,
            detail=f"函数 {func_name} 不在白名单中，允许的前缀: {ALLOWED_PREFIXES}"
        )

    func = getattr(ak, func_name, None)
    if func is None:
        raise HTTPException(status_code=404, detail=f"akshare 无此函数: {func_name}")
    if not callable(func):
        raise HTTPException(status_code=400, detail=f"{func_name} 不可调用")

    # 从 query params 提取参数
    params = dict(request.query_params)

    try:
        logger.info("Generic proxy call", func=func_name, params=params)
        df = _cached_call(func_name, func, **params)
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except HTTPException:
        raise
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("Generic proxy failed", func=func_name, params=params,
                     error=str(e), trace=traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  启动
# =====================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9898, log_level="info")
