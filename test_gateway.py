"""
AKShare Gateway 全接口测试脚本 v2.0

用途：部署到新服务器后，验证各第三方 API 是否可正常调通。

测试模式：
  1. 直连模式 (--mode direct)  — 直接调 akshare，验证服务器网络是否能访问东财/新浪等
  2. 网关模式 (--mode gateway) — 调用已启动的 gateway HTTP 接口，验证网关服务是否正常
  3. 全部模式 (--mode all)     — 先测直连，再测网关（默认）

用法：
  python test_gateway.py                          # 全部测试
  python test_gateway.py --mode direct             # 只测直连
  python test_gateway.py --mode gateway            # 只测网关
  python test_gateway.py --gateway http://localhost:9898  # 指定网关地址
  python test_gateway.py --fast                    # 快速模式（跳过慢接口）
"""

import argparse
import json
import sys
import time
import traceback
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# ANSI 颜色
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _ok(msg: str):
    print(f"  {GREEN}✅ PASS{RESET}  {msg}")


def _fail(msg: str):
    print(f"  {RED}❌ FAIL{RESET}  {msg}")


def _warn(msg: str):
    print(f"  {YELLOW}⚠️  WARN{RESET}  {msg}")


def _section(title: str):
    print(f"\n{BOLD}{CYAN}{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}{RESET}\n")


# =====================================================================
#  直连测试（直接调 akshare）
# =====================================================================

def test_direct_akshare(fast: bool = False) -> List[Dict]:
    """直接调用 akshare 各函数，验证服务器网络连通性"""
    _section("直连测试 — 直接调用 akshare（验证网络连通性）")

    try:
        import akshare as ak
        print(f"  akshare version: {ak.__version__}\n")
    except ImportError:
        print(f"  {RED}akshare 未安装！请先 pip install akshare{RESET}")
        return [{"name": "akshare_import", "status": "FAIL", "error": "not installed"}]

    today = datetime.now().strftime("%Y%m%d")
    # 如果今天是周末，用上周五
    dow = datetime.now().weekday()
    if dow == 5:  # 周六
        trade_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    elif dow == 6:  # 周日
        trade_date = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")
    else:
        trade_date = today

    hist_start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    # 测试用例：(名称, 函数, 参数, 预期最少行数, 是否慢接口)
    test_cases: List[Tuple[str, str, dict, int, bool]] = [
        # ---- 核心高频接口 ----
        ("A股实时行情 (stock_zh_a_spot_em)",
         "stock_zh_a_spot_em", {}, 3000, False),

        ("个股历史K线 (stock_zh_a_hist) - 600519",
         "stock_zh_a_hist",
         {"symbol": "600519", "period": "daily",
          "start_date": hist_start, "end_date": today, "adjust": "qfq"},
         10, False),

        ("A股代码名称 (stock_info_a_code_name)",
         "stock_info_a_code_name", {}, 3000, False),

        # ---- 北向资金 ----
        ("北向持仓排名 (stock_hsgt_hold_stock_em)",
         "stock_hsgt_hold_stock_em",
         {"market": "北向", "indicator": "今日排行"}, 10, False),

        # ---- 资金流向 ----
        ("个股资金流排名 (stock_individual_fund_flow_rank)",
         "stock_individual_fund_flow_rank",
         {"indicator": "今日"}, 100, False),

        # ---- 涨跌停 ----
        ("涨停股池 (stock_zt_pool_em)",
         "stock_zt_pool_em", {"date": trade_date}, 1, False),

        # ---- 概念板块 ----
        ("东财概念板块 (board_concept_name_em)",
         "stock_board_concept_name_em", {}, 100, False),

        # ---- 港股行情（慢接口）----
        ("港股实时行情 (stock_hk_spot_em)",
         "stock_hk_spot_em", {}, 1000, True),

        # ---- 估值指标（慢接口）----
        ("个股估值指标 (stock_a_indicator_lg) - 600519",
         "stock_a_indicator_lg", {"symbol": "600519"}, 100, True),

        # ---- 个股详情 ----
        ("个股详情 (stock_individual_info_em) - 600519",
         "stock_individual_info_em", {"symbol": "600519"}, 1, False),

        # ---- 概念板块成份 ----
        ("概念板块成份 (board_concept_cons_em) - 人工智能",
         "stock_board_concept_cons_em", {"symbol": "人工智能"}, 10, True),

        # ---- 新闻快讯 ----
        ("财经快讯-东财 (stock_info_global_em)",
         "stock_info_global_em", {}, 5, False),

        ("财经快讯-财联社 (stock_info_global_cls)",
         "stock_info_global_cls", {}, 5, True),

        # ---- 同花顺 ----
        ("同花顺概念板块 (board_concept_name_ths)",
         "stock_board_concept_name_ths", {}, 100, True),
    ]

    results = []
    passed = 0
    failed = 0
    skipped = 0

    for name, func_name, kwargs, min_rows, is_slow in test_cases:
        if fast and is_slow:
            _warn(f"{name} — 跳过（快速模式）")
            skipped += 1
            results.append({"name": name, "status": "SKIP"})
            continue

        start = time.time()
        try:
            func = getattr(ak, func_name)
            df = func(**kwargs)
            elapsed = time.time() - start
            rows = len(df) if df is not None else 0

            if rows >= min_rows:
                _ok(f"{name}  —  {rows} 行  ({elapsed:.1f}s)")
                results.append({"name": name, "status": "PASS",
                                "rows": rows, "time": round(elapsed, 1)})
                passed += 1
            elif rows > 0:
                _warn(f"{name}  —  {rows} 行 (预期 ≥{min_rows})  ({elapsed:.1f}s)")
                results.append({"name": name, "status": "WARN",
                                "rows": rows, "time": round(elapsed, 1)})
                passed += 1  # 有数据算通过
            else:
                _fail(f"{name}  —  返回空  ({elapsed:.1f}s)")
                results.append({"name": name, "status": "FAIL",
                                "error": "empty result", "time": round(elapsed, 1)})
                failed += 1

        except Exception as e:
            elapsed = time.time() - start
            err_msg = str(e)[:120]
            _fail(f"{name}  —  {err_msg}  ({elapsed:.1f}s)")
            results.append({"name": name, "status": "FAIL",
                            "error": err_msg, "time": round(elapsed, 1)})
            failed += 1

        # 请求间延迟，避免触发反爬
        time.sleep(1.0)

    print(f"\n  📊 直连总结：{GREEN}{passed} 通过{RESET} / "
          f"{RED}{failed} 失败{RESET} / "
          f"{YELLOW}{skipped} 跳过{RESET}")

    return results


# =====================================================================
#  网关测试（调 gateway HTTP 接口）
# =====================================================================

def _gateway_get(base_url: str, path: str, params: dict = None,
                 timeout: int = 60) -> Tuple[dict, float]:
    """调用 gateway 接口，返回 (JSON body, 耗时秒)"""
    url = f"{base_url.rstrip('/')}{path}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{query}"

    headers = {"Accept": "application/json", "User-Agent": "test-gateway/1.0"}
    req = urllib.request.Request(url, headers=headers)

    start = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    elapsed = time.time() - start
    return body, elapsed


def test_gateway_api(base_url: str, fast: bool = False) -> List[Dict]:
    """测试 gateway 各 HTTP 接口"""
    _section(f"网关测试 — {base_url}")

    # 健康检查
    try:
        body, elapsed = _gateway_get(base_url, "/health")
        if body.get("status") == "ok":
            version = body.get("version", "?")
            _ok(f"健康检查  —  v{version}  ({elapsed:.1f}s)")
        else:
            _fail(f"健康检查  —  status={body.get('status')}")
            return [{"name": "health", "status": "FAIL"}]
    except Exception as e:
        _fail(f"健康检查  —  {str(e)[:100]}")
        print(f"\n  {RED}网关不可用，跳过后续测试{RESET}")
        return [{"name": "health", "status": "FAIL", "error": str(e)[:100]}]

    today = datetime.now().strftime("%Y%m%d")
    dow = datetime.now().weekday()
    if dow == 5:
        trade_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    elif dow == 6:
        trade_date = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")
    else:
        trade_date = today

    hist_start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    # 测试用例：(名称, 路径, 参数, 预期最少行数, 是否慢)
    test_cases: List[Tuple[str, str, dict, int, bool]] = [
        ("A股实时行情",
         "/api/stock/zh_a_spot_em", {}, 3000, False),

        ("个股K线-600519",
         "/api/stock/zh_a_hist",
         {"symbol": "600519", "period": "daily",
          "start_date": hist_start, "end_date": today, "adjust": "qfq"},
         10, False),

        ("A股代码名称",
         "/api/stock/info_a_code_name", {}, 3000, False),

        ("北向持仓排名",
         "/api/stock/hsgt_hold_stock_em",
         {"market": "北向", "indicator": "今日排行"}, 10, False),

        ("资金流排名",
         "/api/stock/individual_fund_flow_rank",
         {"indicator": "今日"}, 100, False),

        ("涨停股池",
         "/api/stock/zt_pool_em", {"date": trade_date}, 1, False),

        ("东财概念板块",
         "/api/stock/board_concept_name_em", {}, 100, False),

        ("港股行情",
         "/api/stock/hk_spot_em", {}, 1000, True),

        ("个股估值-600519",
         "/api/stock/a_indicator_lg", {"symbol": "600519"}, 100, True),

        ("个股详情-600519",
         "/api/stock/individual_info_em", {"symbol": "600519"}, 1, False),

        ("概念成份-人工智能",
         "/api/stock/board_concept_cons_em", {"symbol": "人工智能"}, 10, True),

        ("快讯-东财",
         "/api/stock/info_global_em", {}, 5, False),

        ("快讯-财联社",
         "/api/stock/info_global_cls", {}, 5, True),

        ("同花顺概念板块",
         "/api/stock/board_concept_name_ths", {}, 100, True),

        # 通用代理测试
        ("通用代理-K线",
         "/api/akshare/stock_zh_a_hist",
         {"symbol": "000001", "period": "daily",
          "start_date": hist_start, "end_date": today, "adjust": "qfq"},
         10, True),
    ]

    results = [{"name": "health", "status": "PASS"}]
    passed = 1  # health already passed
    failed = 0
    skipped = 0

    for name, path, params, min_rows, is_slow in test_cases:
        if fast and is_slow:
            _warn(f"{name} — 跳过（快速模式）")
            skipped += 1
            results.append({"name": name, "status": "SKIP"})
            continue

        try:
            body, elapsed = _gateway_get(base_url, path, params)
            count = body.get("count", 0)
            status = body.get("status", "?")

            if status == "ok" and count >= min_rows:
                _ok(f"{name}  —  {count} 行  ({elapsed:.1f}s)")
                results.append({"name": name, "status": "PASS",
                                "rows": count, "time": round(elapsed, 1)})
                passed += 1
            elif status == "ok" and count > 0:
                _warn(f"{name}  —  {count} 行 (预期 ≥{min_rows})  ({elapsed:.1f}s)")
                results.append({"name": name, "status": "WARN",
                                "rows": count, "time": round(elapsed, 1)})
                passed += 1
            elif status == "ok":
                _fail(f"{name}  —  返回空  ({elapsed:.1f}s)")
                results.append({"name": name, "status": "FAIL",
                                "error": "empty", "time": round(elapsed, 1)})
                failed += 1
            else:
                _fail(f"{name}  —  status={status}  ({elapsed:.1f}s)")
                results.append({"name": name, "status": "FAIL",
                                "error": f"status={status}"})
                failed += 1

        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            _fail(f"{name}  —  HTTP {e.code}: {err_body[:80]}")
            results.append({"name": name, "status": "FAIL",
                            "error": f"HTTP {e.code}"})
            failed += 1

        except Exception as e:
            _fail(f"{name}  —  {str(e)[:100]}")
            results.append({"name": name, "status": "FAIL",
                            "error": str(e)[:100]})
            failed += 1

        time.sleep(0.5)

    # 统计接口
    try:
        body, _ = _gateway_get(base_url, "/stats")
        cache_info = body.get("cache", {})
        anti_info = body.get("anti_scrape", {})
        print(f"\n  📈 网关状态:")
        print(f"     缓存: {cache_info.get('size', 0)} 条, "
              f"命中率 {cache_info.get('hit_rate', 'N/A')}")
        if anti_info:
            print(f"     反爬: 连续错误 {anti_info.get('consecutive_errors', 0)}, "
                  f"Session已Patch: {anti_info.get('session_patched', '?')}")
    except Exception:
        pass

    print(f"\n  📊 网关总结：{GREEN}{passed} 通过{RESET} / "
          f"{RED}{failed} 失败{RESET} / "
          f"{YELLOW}{skipped} 跳过{RESET}")

    return results


# =====================================================================
#  反爬验证测试
# =====================================================================

def test_anti_scrape_fingerprint():
    """验证 Session 指纹注入是否生效"""
    _section("反爬验证 — Session 指纹注入检查")

    try:
        import requests
    except ImportError:
        _warn("requests 未安装，跳过验证")
        return

    # 检查 Session.request 是否被 patch
    session = requests.Session()
    original_name = getattr(requests.Session.request, "__name__", "?")

    # 尝试 import 触发 patch
    try:
        # 如果有 app.py 的 patch
        import app  # noqa
    except Exception:
        pass

    patched_name = getattr(requests.Session.request, "__name__", "?")

    if patched_name == "_patched_request":
        _ok("Session.request 已被 patch（浏览器指纹注入已生效）")
    else:
        _warn(f"Session.request 函数名: {patched_name}（可能未 patch）")

    # 验证 headers 是否正常注入
    print(f"\n  随机生成的浏览器指纹示例:")
    try:
        from app import _build_browser_headers
        headers = _build_browser_headers()
        for key in ["User-Agent", "Referer", "sec-ch-ua", "Sec-Fetch-Mode"]:
            val = headers.get(key, "(未设置)")
            if len(val) > 60:
                val = val[:60] + "..."
            print(f"    {key}: {val}")
    except Exception:
        _warn("无法导入 _build_browser_headers，请确保在 gateway 目录下运行")


# =====================================================================
#  主入口
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="AKShare Gateway 全接口测试脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python test_gateway.py                              # 全部测试
  python test_gateway.py --mode direct                # 只测 akshare 直连
  python test_gateway.py --mode gateway               # 只测网关接口
  python test_gateway.py --gateway http://1.2.3.4:9898  # 指定网关地址
  python test_gateway.py --fast                       # 快速模式（跳过慢接口）
        """
    )
    parser.add_argument("--mode", choices=["direct", "gateway", "all"],
                        default="all", help="测试模式 (默认: all)")
    parser.add_argument("--gateway", default="http://localhost:9898",
                        help="网关地址 (默认: http://localhost:9898)")
    parser.add_argument("--fast", action="store_true",
                        help="快速模式，跳过耗时较长的接口")

    args = parser.parse_args()

    print(f"\n{BOLD}🔍 AKShare Gateway 全接口测试{RESET}")
    print(f"   时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   模式: {args.mode}")
    if args.mode in ("gateway", "all"):
        print(f"   网关: {args.gateway}")
    print(f"   快速: {'是' if args.fast else '否'}")

    all_results = []
    total_pass = 0
    total_fail = 0

    # 直连测试
    if args.mode in ("direct", "all"):
        results = test_direct_akshare(fast=args.fast)
        all_results.extend(results)
        total_pass += sum(1 for r in results if r["status"] in ("PASS", "WARN"))
        total_fail += sum(1 for r in results if r["status"] == "FAIL")

    # 网关测试
    if args.mode in ("gateway", "all"):
        results = test_gateway_api(args.gateway, fast=args.fast)
        all_results.extend(results)
        total_pass += sum(1 for r in results if r["status"] in ("PASS", "WARN"))
        total_fail += sum(1 for r in results if r["status"] == "FAIL")

    # 反爬验证（只在本地运行时测试）
    if args.mode in ("direct", "all"):
        test_anti_scrape_fingerprint()

    # 最终报告
    _section("最终报告")

    # 失败项汇总
    failed_items = [r for r in all_results if r["status"] == "FAIL"]
    if failed_items:
        print(f"  {RED}失败的接口:{RESET}")
        for r in failed_items:
            error = r.get("error", "unknown")
            print(f"    ❌ {r['name']}  —  {error}")
        print()

    # 总结
    if total_fail == 0:
        print(f"  {GREEN}{BOLD}🎉 全部通过！共 {total_pass} 个接口测试成功{RESET}")
        exit_code = 0
    else:
        print(f"  {BOLD}通过: {GREEN}{total_pass}{RESET}{BOLD} / "
              f"失败: {RED}{total_fail}{RESET}")
        exit_code = 1

    print()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
