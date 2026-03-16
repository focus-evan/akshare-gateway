"""
AKShare Gateway API 测试脚本

用法:
    # 测试本地起动的网关
    python test_gateway.py

    # 测试指定地址
    python test_gateway.py http://your-server:9898
"""

import sys
import time
import json
import requests


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9898"


def test_endpoint(name: str, path: str, params: dict = None, expect_data: bool = True):
    """测试单个接口"""
    url = f"{BASE_URL}{path}"
    print(f"\n{'='*60}")
    print(f"📡 测试: {name}")
    print(f"   URL: {url}")
    if params:
        print(f"   参数: {params}")

    try:
        start = time.time()
        resp = requests.get(url, params=params, timeout=60)
        elapsed = time.time() - start

        print(f"   状态码: {resp.status_code}")
        print(f"   耗时: {elapsed:.2f}s")

        if resp.status_code == 200:
            body = resp.json()
            count = body.get("count", 0)
            data = body.get("data", [])
            print(f"   数据行数: {count}")

            if data and len(data) > 0:
                # 显示前2条数据的字段
                print(f"   字段: {list(data[0].keys())}")
                for i, row in enumerate(data[:2]):
                    # 截断过长的值
                    short = {k: (str(v)[:50] if len(str(v)) > 50 else v) for k, v in row.items()}
                    print(f"   示例[{i}]: {json.dumps(short, ensure_ascii=False)[:200]}")
                print(f"   ✅ 通过")
            elif not expect_data:
                print(f"   ⚠️ 无数据（可能正常）")
            else:
                print(f"   ⚠️ 返回空数据")
        else:
            print(f"   ❌ 失败: {resp.text[:200]}")

    except requests.exceptions.ConnectionError:
        print(f"   ❌ 连接失败，请确认网关是否已启动")
    except Exception as e:
        print(f"   ❌ 异常: {e}")


def main():
    print(f"🚀 AKShare Gateway 接口测试")
    print(f"   目标: {BASE_URL}")

    # 1. 健康检查
    test_endpoint("健康检查", "/health")

    # 2. A股实时行情（最常用，也是最容易被反爬的接口）
    test_endpoint("A股实时行情 (stock_zh_a_spot_em)", "/api/stock/zh_a_spot_em")

    # 3. 个股历史K线
    test_endpoint(
        "个股历史K线 (stock_zh_a_hist)",
        "/api/stock/zh_a_hist",
        params={
            "symbol": "600519",
            "period": "daily",
            "start_date": "20241201",
            "end_date": "20241231",
            "adjust": "qfq",
        },
    )

    # 4. 涨停股池
    from datetime import datetime, timedelta
    # 使用前一个交易日
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    test_endpoint(
        "涨停股池 (stock_zt_pool_em)",
        "/api/stock/zt_pool_em",
        params={"date": yesterday},
        expect_data=False,  # 非交易日可能无数据
    )

    # 5. 概念板块列表
    test_endpoint("概念板块列表 (board_concept_name_em)", "/api/stock/board_concept_name_em")

    # 6. 全球财经快讯
    test_endpoint("全球财经快讯-东财 (info_global_em)", "/api/stock/info_global_em")

    # 7. A股代码名称
    test_endpoint("A股代码名称 (info_a_code_name)", "/api/stock/info_a_code_name")

    # 8. 通用代理
    test_endpoint("通用代理测试 (stock_info_a_code_name)", "/api/akshare/stock_info_a_code_name")

    print(f"\n{'='*60}")
    print(f"🏁 测试完成")


if __name__ == "__main__":
    main()
