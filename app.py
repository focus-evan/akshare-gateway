"""
AKShare Gateway — 统一第三方数据接口网关平台 v3.0

部署在独立服务器/Docker 中，供 ai-stock 主服务通过内网调用。
解决云服务器直接调用 akshare 时被东方财富反爬封锁的问题。

功能:
  1. 已注册接口 — 参数校验 + 定制化处理
  2. 通用代理 — 任意 akshare 函数代理（白名单保护）
  3. TTL 缓存 — 减少对东财的请求频率
  4. 请求限流 — 自动随机延迟，防止触发风控
  5. 统计监控 — 请求次数、缓存命中率、接口耗时
  6. **反爬绕过** — Session伪装、Header轮换、指纹随机化、退避策略

所有接口返回 JSON 格式: {"status":"ok","count":N,"data":[...]}
"""

import hashlib
import json
import math
import os
import random
import ssl
import time
import traceback
import threading
from collections import defaultdict
from datetime import datetime, date as date_type, time as time_type
from typing import Any, Dict, List, Optional

import akshare as ak
import pandas as pd
import requests
import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger()


# =====================================================================
#  反爬伪装引擎（核心优化）
# =====================================================================

# User-Agent 池 — 模拟真实浏览器分布
_UA_POOL = [
    # Chrome 125 (Windows 11)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 124 (Windows 10)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 125 (macOS)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Firefox 126 (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
    "Gecko/20100101 Firefox/126.0",
    # Edge 124 (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Chrome 123 (Linux)
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Safari 17 (macOS)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Chrome 126 (Windows 11) - newer
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

# Referer 池 — 模拟从不同页面跳转
_REFERER_POOL = [
    "https://quote.eastmoney.com/",
    "https://data.eastmoney.com/",
    "https://guba.eastmoney.com/",
    "https://so.eastmoney.com/",
    "https://www.eastmoney.com/",
    "https://fund.eastmoney.com/",
    "https://choice.eastmoney.com/",
]

# Accept-Language 池
_LANG_POOL = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "zh-CN,zh;q=0.9",
    "zh-CN,zh-TW;q=0.9,zh;q=0.8,en;q=0.7",
]


def _build_browser_headers() -> Dict[str, str]:
    """
    构建高仿真浏览器请求头

    每次请求随机组合 UA + Referer + Accept 等，
    模拟不同浏览器/不同页面的访问行为。
    """
    ua = random.choice(_UA_POOL)
    is_chrome = "Chrome" in ua and "Edg" not in ua

    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": random.choice(_LANG_POOL),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": random.choice(_REFERER_POOL),
        "Cache-Control": random.choice(["no-cache", "max-age=0"]),
        "Upgrade-Insecure-Requests": "1",
    }

    # Chrome 特有的 sec- 系列头（东财检测的重点）
    if is_chrome:
        headers.update({
            "sec-ch-ua": random.choice([
                '"Chromium";v="125", "Google Chrome";v="125", "Not.A/Brand";v="24"',
                '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                '"Chromium";v="126", "Google Chrome";v="126", "Not/A)Brand";v="8"',
            ]),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": random.choice(['"Windows"', '"macOS"', '"Linux"']),
            "Sec-Fetch-Dest": random.choice(["document", "empty"]),
            "Sec-Fetch-Mode": random.choice(["navigate", "cors", "no-cors"]),
            "Sec-Fetch-Site": random.choice(["same-origin", "same-site", "none"]),
        })

    return headers


def _patch_akshare_session():
    """
    Monkey-patch akshare 底层的 requests.Session，
    让每次请求自动携带随机浏览器 headers。

    akshare 内部使用 requests.get / requests.Session 访问东财/新浪等，
    默认的 User-Agent 是 python-requests/x.x.x，极易被识别为爬虫。
    通过替换 Session.request 方法，在底层注入真实浏览器指纹。
    """
    _original_request = requests.Session.request
    _original_init = requests.Session.__init__

    def _patched_init(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        # 设置连接池参数：禁止连接复用过久，减少被封时的影响面
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=0,  # 由我们自己的 _retry 控制重试
            pool_block=False,
        )
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def _patched_request(self, method, url, **kwargs):
        # 只对东财/新浪/同花顺等金融数据源注入 headers
        target_domains = (
            "eastmoney.com", "push2.eastmoney.com",
            "push2his.eastmoney.com",
            "datacenter.eastmoney.com", "data.eastmoney.com",
            "datacenter-web.eastmoney.com",
            "quote.eastmoney.com", "emweb.securities.eastmoney.com",
            "sina.com", "sinajs.cn",
            "10jqka.com.cn",  # 同花顺
        )

        is_target = any(d in str(url) for d in target_domains)

        if is_target:
            # 注入浏览器 headers（不覆盖已有的）
            browser_headers = _build_browser_headers()
            if "headers" not in kwargs or kwargs["headers"] is None:
                kwargs["headers"] = {}
            for key, value in browser_headers.items():
                if key not in kwargs["headers"]:
                    kwargs["headers"][key] = value

            # 确保 timeout 合理
            if "timeout" not in kwargs:
                kwargs["timeout"] = 30

            # 对东财接口禁用 SSL 验证（部分被封时 SSL 握手会超时）
            if "verify" not in kwargs:
                kwargs["verify"] = True

        return _original_request(self, method, url, **kwargs)

    requests.Session.__init__ = _patched_init
    requests.Session.request = _patched_request
    logger.info("akshare Session patched with browser fingerprint injection")


# 启动时就 patch
_patch_akshare_session()


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

    def cleanup(self):
        """清理过期缓存"""
        with self._lock:
            now = time.time()
            expired = [k for k, (_, t) in self._store.items() if now >= t]
            for k in expired:
                del self._store[k]
            return len(expired)

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
    if func_name in CACHE_TTL:
        return CACHE_TTL[func_name]
    short = func_name.replace("stock_", "")
    if short in CACHE_TTL:
        return CACHE_TTL[short]
    return CACHE_TTL["default"]


# =====================================================================
#  智能限流 & 退避策略
# =====================================================================

_last_request_time = 0.0
_rate_lock = threading.Lock()
_consecutive_errors = 0
_error_lock = threading.Lock()

# 基础请求间隔（秒）
MIN_REQUEST_INTERVAL = 0.5  # 从 0.3 提升到 0.5，减少触发频率

# 退避策略参数
MAX_BACKOFF_INTERVAL = 30.0  # 最大退避到 30 秒
BACKOFF_DECAY_TIME = 300     # 5 分钟无错误后重置退避


def _smart_rate_limit():
    """
    智能限流：正常时温和延迟，遇到反爬时指数退避

    - 正常：0.5~1.5s 随机间隔
    - 连续出错 1 次：2~4s
    - 连续出错 2 次：4~8s
    - 连续出错 3+ 次：8~30s
    """
    global _last_request_time
    with _rate_lock:
        now = time.time()

        # 基于连续错误数计算退避间隔
        with _error_lock:
            err_count = _consecutive_errors

        if err_count == 0:
            interval = MIN_REQUEST_INTERVAL + random.uniform(0.0, 1.0)
        elif err_count == 1:
            interval = 2.0 + random.uniform(0.0, 2.0)
        elif err_count == 2:
            interval = 4.0 + random.uniform(0.0, 4.0)
        else:
            interval = min(MAX_BACKOFF_INTERVAL,
                           8.0 * (1.5 ** (err_count - 3)) + random.uniform(0.0, 5.0))

        elapsed = now - _last_request_time
        if elapsed < interval:
            delay = interval - elapsed
            logger.debug("Smart rate limit", delay=round(delay, 1),
                         error_level=err_count)
            time.sleep(delay)

        _last_request_time = time.time()


def _record_success():
    """记录成功请求，重置退避"""
    global _consecutive_errors
    with _error_lock:
        if _consecutive_errors > 0:
            logger.info("Anti-scrape backoff reset",
                        previous_errors=_consecutive_errors)
        _consecutive_errors = 0


def _record_error():
    """记录失败请求，增加退避"""
    global _consecutive_errors
    with _error_lock:
        _consecutive_errors += 1
        logger.warning("Anti-scrape backoff increased",
                       consecutive_errors=_consecutive_errors,
                       next_interval=f"{min(MAX_BACKOFF_INTERVAL, 2 ** _consecutive_errors):.0f}s")


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
    description="统一第三方数据接口网关 v3.0 — 缓存 + 限流 + 反爬绕过 + 监控",
    version="3.0.0",
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

class _SafeJSONEncoder(json.JSONEncoder):
    """处理 date/datetime/time/Timestamp 等不可直接序列化的类型"""
    def default(self, obj):
        if isinstance(obj, (datetime, date_type)):
            return obj.isoformat()
        if isinstance(obj, time_type):
            return obj.isoformat()
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        if isinstance(obj, (pd.Timedelta,)):
            return str(obj)
        if hasattr(obj, 'item'):  # numpy int/float
            return obj.item()
        return super().default(obj)


def _safe_serialize(obj):
    """安全序列化，处理所有特殊类型，包括 NaN/Inf/date/time"""
    def _clean(o):
        """递归清理不可序列化的值"""
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()}
        elif isinstance(o, (list, tuple)):
            return [_clean(item) for item in o]
        elif isinstance(o, float):
            if math.isnan(o) or math.isinf(o):
                return None
            return o
        elif isinstance(o, (datetime, date_type)):
            return o.isoformat()
        elif isinstance(o, time_type):
            return o.isoformat()
        elif isinstance(o, pd.Timestamp):
            return o.isoformat()
        elif isinstance(o, pd.Timedelta):
            return str(o)
        elif hasattr(o, 'item'):  # numpy scalar
            val = o.item()
            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                return None
            return val
        return o

    cleaned = _clean(obj)
    return json.loads(json.dumps(cleaned, ensure_ascii=False))


def _df_to_response(df: Optional[pd.DataFrame], name: str = "data") -> Response:
    """将 DataFrame 转为 JSON 响应（使用 Response 绕过 JSONResponse 的 allow_nan=False）"""
    if df is None or df.empty:
        body = json.dumps({"status": "ok", "count": 0, "data": []}, ensure_ascii=False)
        return Response(content=body, media_type="application/json")

    # 处理 NaN / Inf → None
    import numpy as np
    df = df.replace([np.inf, -np.inf], None)
    df = df.where(pd.notnull(df), None)

    # 将 Timestamp 列转为字符串
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].astype(str).replace('NaT', None)

    records = df.to_dict(orient="records")

    # 递归清理所有不可序列化的值（NaN/Inf/date/time/numpy）
    safe_records = _safe_serialize(records)

    # 手动 json.dumps + default=str 兼容万一显漏的类型
    body = json.dumps(
        {"status": "ok", "count": len(safe_records), "data": safe_records},
        ensure_ascii=False,
        default=str,  # 最终兆底：任何无法序列化的类型都转为字符串
    )
    return Response(content=body, media_type="application/json")


def _reset_connections():
    """
    强制重置所有 HTTP 连接池。
    当被反爬封锁时，旧的TCP连接可能已被服务端标记，
    需要关闭并新建连接才能绕过。
    """
    try:
        import urllib3
        urllib3.disable_warnings()
    except ImportError:
        pass

    # 关闭 requests 默认 Session 的连接池
    try:
        for attr_name in dir(requests):
            obj = getattr(requests, attr_name, None)
            if isinstance(obj, requests.Session):
                obj.close()
    except Exception:
        pass

    logger.info("Connection pools reset")


def _retry(func, *args, max_retries: int = 3, delay: float = 2.0, **kwargs):
    """
    带重试 + 指数退避 + 反爬感知的函数调用

    每次重试前会: 切换 headers、增加延迟、重置连接池、记录错误到退避系统
    """
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            result = func(*args, **kwargs)
            _record_success()
            return result
        except Exception as e:
            last_err = e
            error_str = str(e).lower()

            # 检测是否被反爬封锁
            is_anti_scrape = any(kw in error_str for kw in [
                "connection aborted", "remote end closed",
                "remotedisconnected", "connectionreset",
                "443", "forbidden", "too many requests",
                "456",  # HTTP 456 (自定义反爬状态码)
                "验证", "频繁", "限制", "block",
            ])

            _record_error()

            logger.warning(
                "akshare call failed, retrying",
                func=func.__name__,
                attempt=attempt,
                is_anti_scrape=is_anti_scrape,
                error=str(e),
            )

            if attempt < max_retries:
                # 反爬时重置连接池（强制新建TCP连接）
                if is_anti_scrape:
                    _reset_connections()

                # 指数退避 + 随机抖动，反爬时退避更久
                base_delay = delay * (2 ** (attempt - 1))
                if is_anti_scrape:
                    base_delay *= 2  # 反爬封锁时加倍退避
                jitter = random.uniform(0, base_delay * 0.5)
                actual_delay = base_delay + jitter
                logger.info("Retry backoff", delay=round(actual_delay, 1),
                            attempt=attempt)
                time.sleep(actual_delay)

    raise last_err


def _cached_call(func_name: str, func, *args, **kwargs) -> pd.DataFrame:
    """
    带缓存 + 智能限流的 akshare 调用

    1. 检查缓存
    2. 未命中则智能限流 + 调用 + 缓存结果
    3. 失败时尝试返回过期缓存（degraded mode）
    """
    key = _cache_key(func_name, **kwargs)
    cached = cache.get(key)
    if cached is not None:
        logger.debug("Cache hit", func=func_name, key=key)
        return cached

    # 智能限流（根据错误频率动态调整间隔）
    _smart_rate_limit()

    try:
        # 调用
        df = _retry(func, *args, **kwargs)

        # 缓存
        ttl = _get_ttl(func_name)
        if df is not None and not df.empty:
            cache.set(key, df, ttl)
            logger.debug("Cached result", func=func_name, key=key,
                         ttl=ttl, rows=len(df))

        return df

    except Exception as e:
        # 降级：尝试返回过期缓存（宁可用旧数据也不返回空）
        logger.warning("Call failed, checking stale cache", func=func_name,
                        error=str(e))
        # 直接检查 store 绕过 TTL
        with cache._lock:
            if key in cache._store:
                stale_data, _ = cache._store[key]
                logger.warning("Returning STALE cached data (degraded mode)",
                               func=func_name, key=key)
                return stale_data

        raise


# =====================================================================
#  定时缓存清理
# =====================================================================

def _start_cache_cleanup():
    """后台线程定期清理过期缓存"""
    def _cleanup_loop():
        while True:
            time.sleep(300)  # 每 5 分钟清理一次
            try:
                removed = cache.cleanup()
                if removed > 0:
                    logger.debug("Cache cleanup", removed=removed)
            except Exception:
                pass

    t = threading.Thread(target=_cleanup_loop, daemon=True)
    t.start()

_start_cache_cleanup()


# =====================================================================
#  多数据源备用引擎 (Multi-Source Fallback Engine)
# =====================================================================
#  当东财(Eastmoney)被反爬时，自动切换到新浪/腾讯/同花顺等数据源
# =====================================================================

def _multi_source_call(sources: list, cache_name: str = None,
                       cache_ttl: float = None) -> pd.DataFrame:
    """
    多数据源调用，按优先级依次尝试，直到成功

    Args:
        sources: [(source_name, callable, kwargs_dict), ...]
        cache_name: 缓存使用的键名（统一缓存，不区分数据源）
        cache_ttl: 缓存 TTL，默认使用 CACHE_TTL 配置
    Returns:
        pd.DataFrame
    """
    # 检查缓存
    if cache_name:
        key = _cache_key(cache_name)
        cached = cache.get(key)
        if cached is not None:
            logger.debug("Multi-source cache hit", key=cache_name)
            return cached

    last_err = None
    for source_name, func, kwargs in sources:
        try:
            _smart_rate_limit()
            logger.info("Trying data source", source=source_name)
            df = _retry(func, max_retries=2, delay=1.5, **kwargs)
            if df is not None and not df.empty:
                # 缓存结果
                if cache_name:
                    ttl = cache_ttl or _get_ttl(cache_name)
                    cache.set(_cache_key(cache_name), df, ttl)
                logger.info("Data source success", source=source_name,
                            rows=len(df))
                _record_stat(cache_name or source_name,
                             0, cache_hit=False)
                return df
            else:
                logger.warning("Data source returned empty",
                               source=source_name)
        except Exception as e:
            last_err = e
            logger.warning("Data source failed, trying next",
                           source=source_name, error=str(e)[:200])
            continue

    # 所有数据源都失败，尝试过期缓存
    if cache_name:
        key = _cache_key(cache_name)
        with cache._lock:
            if key in cache._store:
                stale_data, _ = cache._store[key]
                logger.warning("All sources failed, returning stale cache",
                               cache_name=cache_name)
                return stale_data

    if last_err:
        raise last_err
    raise Exception("All data sources failed and no cache available")


# ---- 新浪财经直连 ----

def _sina_a_spot() -> pd.DataFrame:
    """
    新浪财经 A 股实时行情 — 直接 HTTP 调用
    绕过 akshare，直接调用新浪 API
    """
    all_data = []
    headers = _build_browser_headers()
    headers['Referer'] = 'https://finance.sina.com.cn/stock/'

    for page in range(1, 8):  # 最多取7页 ≈ 5000只
        url = (
            f"https://vip.stock.finance.sina.com.cn/quotes_service/api/"
            f"json_v2.php/Market_Center.getHQNodeData"
            f"?page={page}&num=1000&sort=changepercent&asc=0"
            f"&node=hs_a&symbol=&_s_r_a=auto"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                break
            data = resp.json()
            if not data:
                break
            all_data.extend(data)
        except Exception as e:
            logger.warning("Sina spot page failed", page=page, error=str(e))
            break
        time.sleep(random.uniform(0.3, 0.8))

    if not all_data:
        return pd.DataFrame()

    df = pd.DataFrame(all_data)
    # 重命名列以匹配东财格式
    col_map = {
        'symbol': '代码', 'code': '代码', 'name': '名称',
        'trade': '最新价', 'pricechange': '涨跌额',
        'changepercent': '涨跌幅', 'buy': '买入', 'sell': '卖出',
        'settlement': '昨收', 'open': '今开', 'high': '最高',
        'low': '最低', 'volume': '成交量', 'amount': '成交额',
        'ticktime': '更新时间', 'turnoverratio': '换手率',
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    logger.info("Sina A-spot fetched", rows=len(df))
    return df


def _sina_stock_hist(symbol: str, start_date: str = "",
                     end_date: str = "", **kwargs) -> pd.DataFrame:
    """
    新浪财经个股日K线 — 通过 akshare 的新浪接口
    """
    try:
        # akshare 的新浪日K线接口
        func = getattr(ak, 'stock_zh_a_daily', None)
        if func:
            ak_kwargs = {"symbol": f"sz{symbol}" if symbol.startswith(('0', '3')) else f"sh{symbol}"}
            if start_date:
                ak_kwargs["start_date"] = start_date
            if end_date:
                ak_kwargs["end_date"] = end_date
            ak_kwargs["adjust"] = kwargs.get("adjust", "qfq")
            return func(**ak_kwargs)
    except Exception as e:
        logger.warning("Sina stock_zh_a_daily failed", symbol=symbol, error=str(e))

    return pd.DataFrame()


def _tencent_kline(symbol: str, start_date: str = "",
                   end_date: str = "", **kwargs) -> pd.DataFrame:
    """
    腾讯财经个股日K线 — 直接 HTTP 调用
    """
    headers = _build_browser_headers()
    headers['Referer'] = 'https://stockapp.finance.qq.com/'

    prefix = "sz" if symbol.startswith(('0', '3')) else "sh"
    fq = kwargs.get("adjust", "qfq")

    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={prefix}{symbol},day,{start_date},{end_date},500,{fq}"
    )

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        raw = resp.json()

        # 解析腾讯K线数据 — data 可能是 dict 或 list
        data_obj = raw.get('data', {})
        if isinstance(data_obj, list):
            # 部分接口返回 list 格式
            klines = data_obj[0] if data_obj else {}
            if isinstance(klines, dict):
                klines = klines.get(f'{prefix}{symbol}', klines)
        elif isinstance(data_obj, dict):
            klines = data_obj.get(f'{prefix}{symbol}', {})
        else:
            klines = {}

        if isinstance(klines, dict):
            day_data = klines.get(fq + 'day', klines.get('day',
                         klines.get('qfqday', klines.get('hfqday', []))))
        else:
            day_data = []

        if not day_data:
            return pd.DataFrame()

        rows = []
        for item in day_data:
            if isinstance(item, (list, tuple)) and len(item) >= 5:
                rows.append({
                    '日期': item[0],
                    '开盘': float(item[1]),
                    '收盘': float(item[2]),
                    '最高': float(item[3]),
                    '最低': float(item[4]),
                    '成交量': float(item[5]) if len(item) > 5 else 0,
                })

        df = pd.DataFrame(rows)
        logger.info("Tencent kline fetched", symbol=symbol, rows=len(df))
        return df

    except Exception as e:
        logger.warning("Tencent kline failed", symbol=symbol, error=str(e))
        return pd.DataFrame()


def _sina_hk_spot() -> pd.DataFrame:
    """
    新浪财经港股实时行情 — 通过 akshare
    """
    try:
        func = getattr(ak, 'stock_hk_spot', None)
        if func:
            return func()
    except Exception as e:
        logger.warning("Sina HK spot failed", error=str(e))
    return pd.DataFrame()


def _fetch_stock_detail_sina(symbol: str) -> pd.DataFrame:
    """
    新浪财经个股详情 — 直接 HTTP 调用
    """
    headers = _build_browser_headers()
    headers['Referer'] = 'https://finance.sina.com.cn/'

    prefix = "sz" if symbol.startswith(('0', '3')) else "sh"
    url = f"https://hq.sinajs.cn/list={prefix}{symbol}"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = 'gbk'
        text = resp.text
        # 解析: var hq_str_sh600519="贵州茅台,1749.00,...";
        parts = text.split('"')[1].split(',') if '"' in text else []
        if len(parts) >= 32:
            data = {
                'item': ['名称', '今开', '昨收', '最新价', '最高', '最低',
                          '买入', '卖出', '成交量', '成交额', '日期', '时间'],
                'value': [parts[0], parts[1], parts[2], parts[3], parts[4],
                          parts[5], parts[6], parts[7], parts[8], parts[9],
                          parts[30], parts[31]]
            }
            return pd.DataFrame(data)
    except Exception as e:
        logger.warning("Sina stock detail failed", symbol=symbol, error=str(e))
    return pd.DataFrame()




def _fetch_stock_detail_vip_sina(symbol: str) -> pd.DataFrame:
    """
    新浪财经个股详情 — 使用 vip.stock API（更可靠，sinajs 经常 403）
    """
    headers = _build_browser_headers()
    headers['Referer'] = 'https://finance.sina.com.cn/'

    # 方法1: 用 vip.stock API
    try:
        url = (
            f"https://vip.stock.finance.sina.com.cn/quotes_service/api/"
            f"json_v2.php/Market_Center.getHQNodeData"
            f"?page=1&num=1&sort=symbol&asc=1&node=hs_a&symbol={symbol}"
        )
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data and len(data) > 0:
            item = data[0]
            result = {
                'item': ['名称', '总市值', '流通市值', '行业', '上市时间',
                         '每股收益', '市盈率(动)', '市净率', '市销率', '每股净资产'],
                'value': [
                    item.get('name', ''),
                    str(item.get('mktcap', '')),
                    str(item.get('nmc', '')),
                    item.get('trade', ''),
                    '',
                    str(item.get('eps', '')),
                    str(item.get('per', '')),
                    str(item.get('pb', '')),
                    '',
                    str(item.get('bvps', '')),
                ]
            }
            return pd.DataFrame(result)
    except Exception as e:
        logger.warning("Sina vip stock detail failed", symbol=symbol, error=str(e))
    return pd.DataFrame()


def _fetch_stock_detail_tencent(symbol: str) -> pd.DataFrame:
    """
    腾讯财经个股详情 — 直接 HTTP 调用
    """
    headers = _build_browser_headers()
    headers['Referer'] = 'https://stockapp.finance.qq.com/'

    prefix = "sz" if symbol.startswith(('0', '3')) else "sh"

    try:
        url = f"https://qt.gtimg.cn/q={prefix}{symbol}"
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = 'gbk'
        text = resp.text
        # 解析: v_sh600519="1~贵州茅台~600519~1749.00~..."
        parts = text.split('~') if '~' in text else []
        if len(parts) >= 45:
            data = {
                'item': ['名称', '今开', '昨收', '最新价', '最高', '最低',
                         '买入', '卖出', '成交量', '成交额', '总市值', '流通市值'],
                'value': [parts[1], parts[5], parts[4], parts[3], parts[33], parts[34],
                          parts[9], parts[19], parts[6], parts[37],
                          parts[45] if len(parts) > 45 else '',
                          parts[44] if len(parts) > 44 else '']
            }
            return pd.DataFrame(data)
    except Exception as e:
        logger.warning("Tencent stock detail failed", symbol=symbol, error=str(e))
    return pd.DataFrame()


def _fetch_fund_flow_rank_direct(indicator: str = "今日") -> pd.DataFrame:
    """
    直接 HTTP 调用东方财富资金流向排名 API（绕过 akshare）
    """
    headers = _build_browser_headers()
    headers['Referer'] = 'https://data.eastmoney.com/zjlx/detail.html'

    # indicator 映射到 API 参数
    indicator_map = {
        "今日": ("f62", "1"),
        "3日": ("f267", "3"),
        "5日": ("f164", "5"),
        "10日": ("f174", "10"),
    }
    sort_field, days = indicator_map.get(indicator, ("f62", "1"))

    try:
        url = (
            f"https://push2.eastmoney.com/api/qt/clist/get"
            f"?fid={sort_field}&po=1&pz=300&pn=1&np=1"
            f"&fltt=2&invt=2&ut=b2884a393a59ad64002292a3e90d46a5"
            f"&fields=f1,f2,f3,f12,f13,f14,f62,f184,f66,f69,f72,f75,f78,f81,f267,f164,f174"
            f"&fs=m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2"
        )
        resp = requests.get(url, headers=headers, timeout=15)
        data = resp.json()

        if data and data.get('data') and data['data'].get('diff'):
            items = data['data']['diff']
            rows = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                rows.append({
                    '代码': item.get('f12', ''),
                    '名称': item.get('f14', ''),
                    '最新价': item.get('f2'),
                    '涨跌幅': item.get('f3'),
                    '主力净流入': item.get('f62'),
                    '主力净流入占比': item.get('f184'),
                    '超大单净流入': item.get('f66'),
                    '超大单净流入占比': item.get('f69'),
                    '大单净流入': item.get('f72'),
                    '大单净流入占比': item.get('f75'),
                    '中单净流入': item.get('f78'),
                    '中单净流入占比': item.get('f81'),
                })
            if rows:
                df = pd.DataFrame(rows)
                logger.info("Direct fund flow rank fetched", rows=len(df))
                return df
    except Exception as e:
        logger.warning("Direct fund flow rank failed", error=str(e))
    return pd.DataFrame()


def _fetch_concept_cons_direct_em(symbol: str) -> pd.DataFrame:
    """
    直接 HTTP 调用东方财富概念板块成份股 API（绕过 akshare）
    """
    headers = _build_browser_headers()
    headers['Referer'] = 'https://data.eastmoney.com/bkzj/BK0493.html'

    try:
        # 第一步：获取板块代码
        list_url = (
            f"https://push2.eastmoney.com/api/qt/clist/get"
            f"?pn=1&pz=500&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
            f"&fltt=2&invt=2&fid=f3"
            f"&fs=m:90+t:3+f:!50"
            f"&fields=f1,f2,f3,f4,f12,f13,f14"
        )
        resp = requests.get(list_url, headers=headers, timeout=15)
        data = resp.json()

        board_code = None
        if data and data.get('data') and data['data'].get('diff'):
            for item in data['data']['diff']:
                if isinstance(item, dict) and item.get('f14') == symbol:
                    board_code = item.get('f12')
                    break

        if not board_code:
            logger.warning("Concept board code not found", symbol=symbol)
            return pd.DataFrame()

        # 第二步：用板块代码获取成份股
        time.sleep(random.uniform(0.3, 0.8))
        cons_url = (
            f"https://push2.eastmoney.com/api/qt/clist/get"
            f"?pn=1&pz=500&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
            f"&fltt=2&invt=2&fid=f3"
            f"&fs=b:{board_code}+f:!50"
            f"&fields=f1,f2,f3,f4,f5,f6,f7,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f128,f140,f141,f136"
        )
        headers2 = _build_browser_headers()
        headers2['Referer'] = f'https://data.eastmoney.com/bkzj/{board_code}.html'
        resp2 = requests.get(cons_url, headers=headers2, timeout=15)
        data2 = resp2.json()

        if data2 and data2.get('data') and data2['data'].get('diff'):
            rows = []
            for item in data2['data']['diff']:
                if not isinstance(item, dict):
                    continue
                rows.append({
                    '代码': item.get('f12', ''),
                    '名称': item.get('f14', ''),
                    '最新价': item.get('f2'),
                    '涨跌幅': item.get('f3'),
                    '涨跌额': item.get('f4'),
                    '成交量': item.get('f5'),
                    '成交额': item.get('f6'),
                    '振幅': item.get('f7'),
                    '最高': item.get('f15'),
                    '最低': item.get('f16'),
                    '今开': item.get('f17'),
                    '昨收': item.get('f18'),
                    '换手率': item.get('f23'),
                    '市盈率': item.get('f24') if item.get('f24') else None,
                    '市净率': item.get('f25') if item.get('f25') else None,
                })
            if rows:
                df = pd.DataFrame(rows)
                logger.info("Direct concept cons fetched", symbol=symbol, rows=len(df))
                return df
    except Exception as e:
        logger.warning("Direct concept cons failed", symbol=symbol, error=str(e))
    return pd.DataFrame()


# 数据源反爬域名扩展（确保覆盖新浪/腾讯/同花顺）
_REFERER_POOL.extend([
    "https://finance.sina.com.cn/",
    "https://stockapp.finance.qq.com/",
    "https://data.10jqka.com.cn/",
])


# =====================================================================
#  健康检查 & 监控
# =====================================================================

@app.get("/health")
async def health():
    """健康检查"""
    with _error_lock:
        err_count = _consecutive_errors
    return {
        "status": "ok",
        "service": "akshare-gateway",
        "version": "3.0.0",
        "time": datetime.now().isoformat(),
        "cache": cache.stats,
        "anti_scrape": {
            "consecutive_errors": err_count,
            "backoff_level": "normal" if err_count == 0 else
                             "mild" if err_count <= 2 else "aggressive",
        },
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

    with _error_lock:
        err_count = _consecutive_errors

    return {
        "status": "ok",
        "uptime": datetime.now().isoformat(),
        "cache": cache.stats,
        "anti_scrape": {
            "consecutive_errors": err_count,
            "session_patched": True,
        },
        "endpoints": endpoints,
    }


@app.post("/cache/clear")
async def clear_cache():
    """清除所有缓存"""
    cache.clear()
    logger.info("Cache cleared manually")
    return {"status": "ok", "message": "Cache cleared"}


@app.post("/anti-scrape/reset")
async def reset_anti_scrape():
    """手动重置反爬退避状态"""
    global _consecutive_errors
    with _error_lock:
        old = _consecutive_errors
        _consecutive_errors = 0
    logger.info("Anti-scrape backoff manually reset", previous_errors=old)
    return {"status": "ok", "previous_errors": old, "current_errors": 0}


# =====================================================================
#  A 股实时行情
# =====================================================================

@app.get("/api/stock/zh_a_spot_em")
async def stock_zh_a_spot_em():
    """
    获取沪深京 A 股实时行情

    数据源优先级: 东方财富 → 新浪财经
    """
    start = time.time()
    func_name = "stock_zh_a_spot_em"
    try:
        df = _multi_source_call(
            sources=[
                ("eastmoney", ak.stock_zh_a_spot_em, {}),
                ("sina", _sina_a_spot, {}),
            ],
            cache_name=func_name,
        )
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("A股行情全部数据源失败", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
#  港股实时行情
# =====================================================================

@app.get("/api/stock/hk_spot_em")
async def stock_hk_spot_em():
    """
    获取港股实时行情

    数据源优先级: 东方财富 → 新浪财经
    """
    start = time.time()
    func_name = "stock_hk_spot_em"
    try:
        df = _multi_source_call(
            sources=[
                ("eastmoney", ak.stock_hk_spot_em, {}),
                ("sina", _sina_hk_spot, {}),
            ],
            cache_name=func_name,
        )
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("港股行情全部数据源失败", error=str(e))
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

    数据源优先级: 东方财富 → 新浪财经 → 腾讯财经
    """
    start = time.time()
    func_name = "stock_zh_a_hist"
    try:
        em_kwargs = {"symbol": symbol, "period": period, "adjust": adjust}
        if start_date:
            em_kwargs["start_date"] = start_date
        if end_date:
            em_kwargs["end_date"] = end_date

        sina_kwargs = {"symbol": symbol, "start_date": start_date,
                       "end_date": end_date, "adjust": adjust}
        tencent_kwargs = {"symbol": symbol, "start_date": start_date,
                          "end_date": end_date, "adjust": adjust}

        df = _multi_source_call(
            sources=[
                ("eastmoney", ak.stock_zh_a_hist, em_kwargs),
                ("sina", _sina_stock_hist, sina_kwargs),
                ("tencent", _tencent_kline, tencent_kwargs),
            ],
            cache_name=f"{func_name}:{symbol}:{period}:{adjust}",
        )
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("K线全部数据源失败", symbol=symbol, error=str(e))
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

    兼容新旧版本 akshare API 名称变化:
    - stock_a_indicator_lg(symbol=xxx) — 新版按个股查询
    - stock_a_ttm_lyr() — 旧版返回全量数据，需按代码过滤
    """
    start = time.time()
    func_name = "stock_a_indicator_lg"
    try:
        # ---- 阶段1: 尝试带参数的 API（新版 akshare）----
        candidates = [
            ('stock_a_indicator_lg', 'symbol'),
            ('stock_a_lg_indicator', 'symbol'),
            ('stock_a_lg_indicator', 'stock'),
        ]

        last_err = None
        for api_name, param_name in candidates:
            func = getattr(ak, api_name, None)
            if func is None:
                continue
            try:
                df = _cached_call(func_name, func, **{param_name: symbol})
                if df is not None and not df.empty:
                    _record_stat(func_name, (time.time() - start) * 1000)
                    logger.info("stock_a_indicator_lg success",
                                api_used=api_name, param=param_name, symbol=symbol,
                                count=len(df))
                    return _df_to_response(df)
            except TypeError as te:
                logger.warning(f"{api_name}({param_name}=) failed", error=str(te))
                last_err = te
                continue
            except Exception as e:
                last_err = e
                logger.warning(f"{api_name} failed", error=str(e))
                continue

        # ---- 阶段2: stock_a_ttm_lyr() 无参数版本，返回全量再过滤 ----
        ttm_func = getattr(ak, 'stock_a_ttm_lyr', None)
        if ttm_func is not None:
            try:
                cache_key = "stock_a_ttm_lyr_all"
                df_all = _cached_call(cache_key, ttm_func)
                if df_all is not None and not df_all.empty:
                    # 找包含股票代码的列
                    code_col = None
                    for col in df_all.columns:
                        col_lower = str(col).lower()
                        if any(k in col_lower for k in ['code', 'symbol', '代码', 'stock']):
                            code_col = col
                            break
                    if code_col:
                        # 过滤出目标股票（支持 600519 和 sh600519 格式）
                        mask = df_all[code_col].astype(str).str.contains(symbol)
                        df = df_all[mask].copy()
                        if not df.empty:
                            _record_stat(func_name, (time.time() - start) * 1000)
                            logger.info("stock_a_ttm_lyr filtered",
                                        symbol=symbol, rows=len(df))
                            return _df_to_response(df)
                    else:
                        # 没找到代码列，返回全部数据
                        _record_stat(func_name, (time.time() - start) * 1000)
                        logger.info("stock_a_ttm_lyr returned all (no code col found)",
                                    rows=len(df_all))
                        return _df_to_response(df_all)
            except Exception as e:
                logger.warning("stock_a_ttm_lyr() no-arg call failed", error=str(e))
                last_err = e

        if last_err:
            raise last_err
        raise HTTPException(status_code=501,
                            detail="当前 akshare 版本不支持估值指标查询")
    except HTTPException:
        raise
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
    获取个股详细信息

    数据源优先级: 东方财富 → 新浪VIP API → 腾讯财经 → 新浪sinajs
    """
    start = time.time()
    func_name = "stock_individual_info_em"
    try:
        df = _multi_source_call(
            sources=[
                ("eastmoney", ak.stock_individual_info_em, {"symbol": symbol}),
                ("sina_vip", _fetch_stock_detail_vip_sina, {"symbol": symbol}),
                ("tencent", _fetch_stock_detail_tencent, {"symbol": symbol}),
                ("sina_sinajs", _fetch_stock_detail_sina, {"symbol": symbol}),
            ],
            cache_name=f"{func_name}:{symbol}",
        )
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("个股详情全部数据源失败", symbol=symbol, error=str(e))
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

    数据源优先级: akshare东财接口 → 直连东财push2 API
    """
    start = time.time()
    func_name = "stock_individual_fund_flow_rank"
    try:
        df = _multi_source_call(
            sources=[
                ("akshare_em", ak.stock_individual_fund_flow_rank, {"indicator": indicator}),
                ("direct_em", _fetch_fund_flow_rank_direct, {"indicator": indicator}),
            ],
            cache_name=f"{func_name}:{indicator}",
        )
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("资金流排名全部数据源失败", error=str(e))
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
    """
    获取概念板块列表

    数据源优先级: 东方财富 → 同花顺
    """
    start = time.time()
    func_name = "stock_board_concept_name_em"
    try:
        df = _multi_source_call(
            sources=[
                ("eastmoney", ak.stock_board_concept_name_em, {}),
                ("ths", ak.stock_board_concept_name_ths, {}),
            ],
            cache_name=func_name,
        )
        _record_stat(func_name, (time.time() - start) * 1000)
        return _df_to_response(df)
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("概念板块全部数据源失败", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stock/board_concept_cons_em")
async def stock_board_concept_cons_em(
    symbol: str = Query(..., description="概念板块名称"),
):
    """
    获取概念板块成份股

    数据源优先级: 东方财富akshare → 直连东财push2 API → 同花顺
    """
    start = time.time()
    func_name = "stock_board_concept_cons_em"
    try:
        # 数据源1: akshare东财接口
        try:
            _smart_rate_limit()
            df = _retry(ak.stock_board_concept_cons_em, max_retries=2, delay=2.0, symbol=symbol)
            if df is not None and not df.empty:
                cache.set(_cache_key(f"{func_name}:{symbol}"), df, 300)
                _record_stat(func_name, (time.time() - start) * 1000)
                return _df_to_response(df)
        except Exception as em_err:
            logger.warning("东财akshare概念成份失败", symbol=symbol, error=str(em_err))

        # 数据源2: 直连东财push2 API（绕过akshare,用不同的请求头和URL）
        try:
            _smart_rate_limit()
            df = _fetch_concept_cons_direct_em(symbol)
            if df is not None and not df.empty:
                cache.set(_cache_key(f"{func_name}:{symbol}"), df, 300)
                _record_stat(func_name, (time.time() - start) * 1000)
                logger.info("概念成份直连API成功", symbol=symbol, rows=len(df))
                return _df_to_response(df)
        except Exception as direct_err:
            logger.warning("直连东财概念成份失败", symbol=symbol, error=str(direct_err))

        # 数据源3: 同花顺备用（动态查找函数名）
        _smart_rate_limit()
        for ths_name in ['stock_board_concept_cons_ths', 'stock_board_cons_ths']:
            ths_func = getattr(ak, ths_name, None)
            if ths_func is not None:
                try:
                    df = _retry(ths_func, max_retries=2, delay=1.5, symbol=symbol)
                    if df is not None and not df.empty:
                        cache.set(_cache_key(f"{func_name}:{symbol}"), df, 300)
                        _record_stat(func_name, (time.time() - start) * 1000)
                        return _df_to_response(df)
                except Exception as ths_err:
                    logger.warning(f"{ths_name} failed", error=str(ths_err))

        # 最后尝试过期缓存
        stale_key = _cache_key(f"{func_name}:{symbol}")
        with cache._lock:
            if stale_key in cache._store:
                stale_data, _ = cache._store[stale_key]
                logger.warning("概念成份返回过期缓存", symbol=symbol)
                return _df_to_response(stale_data)

        raise Exception(f"概念成份股所有数据源失败: {symbol}")
    except Exception as e:
        _record_stat(func_name, (time.time() - start) * 1000, is_error=True)
        logger.error("概念成份全部数据源失败", symbol=symbol, error=str(e))
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
