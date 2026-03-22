"""
AKShare Gateway v2.0 API 测试脚本

用法:
    python test_gateway.py                          # 测试本地
    python test_gateway.py http://your-server:9898  # 测试指定地址
"""

import sys
import time
import json
import requests


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9898"
PASS = 0
FAIL = 0


def test_endpoint(name: str, path: str, params: dict = None, expect_data: bool = True):
    """测试单个接口"""
    global PASS, FAIL
    url = f"{BASE_URL}{path}"
    print(f"\n{'='*60}")
    print(f"📡 测试: {name}")
    print(f"   URL: {url}")
    if params:
        print(f"   参数: {params}")

    try:
        start = time.time()
        resp = requests.get(url, params=params, timeout=120)
        elapsed = time.time() - start

        print(f"   状态码: {resp.status_code}")
        print(f"   耗时: {elapsed:.2f}s")

        if resp.status_code == 200:
            body = resp.json()
            count = body.get("count", 0)
            data = body.get("data", [])
            print(f"   数据行数: {count}")

            if data and len(data) > 0:
                print(f"   字段: {list(data[0].keys())}")
                for i, row in enumerate(data[:2]):
                    short = {k: (str(v)[:50] if len(str(v)) > 50 else v) for k, v in row.items()}
                    print(f"   示例[{i}]: {json.dumps(short, ensure_ascii=False)[:200]}")
                print(f"   ✅ 通过")
                PASS += 1
            elif not expect_data:
                print(f"   ⚠️ 无数据（可能正常）")
                PASS += 1
            else:
                print(f"   ⚠️ 返回空数据")
                FAIL += 1
        else:
            print(f"   ❌ 失败: {resp.text[:200]}")
            FAIL += 1

    except requests.exceptions.ConnectionError:
        print(f"   ❌ 连接失败，请确认网关是否已启动")
        FAIL += 1
    except Exception as e:
        print(f"   ❌ 异常: {e}")
        FAIL += 1


def main():
    print(f"🚀 AKShare Gateway v2.0 接口测试")
    print(f"   目标: {BASE_URL}")

    # ===== 基础功能 =====
    test_endpoint("健康检查", "/health")

    # ===== 核心接口 =====
    test_endpoint("A股实时行情", "/api/stock/zh_a_spot_em")
    test_endpoint("A股实时行情(缓存命中)", "/api/stock/zh_a_spot_em")  # 第二次应走缓存

    test_endpoint("港股实时行情", "/api/stock/hk_spot_em")

    test_endpoint(
        "个股历史K线",
        "/api/stock/zh_a_hist",
        params={"symbol": "600519", "period": "daily",
                "start_date": "20241201", "end_date": "20241231", "adjust": "qfq"},
    )

    # ===== 新增接口 =====
    test_endpoint(
        "个股PE/PB指标(护城河依赖)",
        "/api/stock/a_indicator_lg",
        params={"symbol": "600519"},
    )

    test_endpoint(
        "个股详细信息",
        "/api/stock/individual_info_em",
        params={"symbol": "600519"},
    )

    test_endpoint(
        "北向资金持仓",
        "/api/stock/hsgt_hold_stock_em",
        params={"market": "北向", "indicator": "今日排行"},
    )

    test_endpoint(
        "资金流向排名",
        "/api/stock/individual_fund_flow_rank",
        params={"indicator": "今日"},
    )

    # ===== 板块 =====
    test_endpoint("概念板块列表(东财)", "/api/stock/board_concept_name_em")

    # ===== 通用代理(万能接口) =====
    test_endpoint(
        "通用代理: stock_a_indicator_lg",
        "/api/akshare/stock_a_indicator_lg",
        params={"symbol": "000300"},
    )

    test_endpoint("A股代码名称", "/api/stock/info_a_code_name")

    # ===== 涨跌停 =====
    from datetime import datetime, timedelta
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    test_endpoint("涨停股池", "/api/stock/zt_pool_em", params={"date": yesterday}, expect_data=False)

    # ===== 监控统计 =====
    test_endpoint("请求统计", "/stats")

    # ===== 结果汇总 =====
    print(f"\n{'='*60}")
    print(f"🏁 测试完成: ✅ {PASS} 通过, ❌ {FAIL} 失败")
    total = PASS + FAIL
    if total > 0:
        print(f"   通过率: {PASS/total*100:.0f}%")


if __name__ == "__main__":
    main()
