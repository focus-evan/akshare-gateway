# AKShare Gateway v2.0

统一第三方数据接口网关平台，为 `ai-stock` 提供稳定的 akshare 数据代理。

部署在独立服务器上，解决阿里云 ECS 被东方财富反爬封锁的问题。

## 架构

```
ai-stock (阿里云ECS，被东财封IP)
    │
    ├── 新浪财经API ──→ 直连（不走东财）
    │
    └── akshare-gateway ──→ 独立服务器/Docker
           │
           ├── TTL缓存层（减少重复请求）
           ├── 请求限流（防风控）  
           ├── 统计监控（/stats）
           │
           └── akshare ──→ 东方财富/同花顺/乐咕乐估等
```

## 功能特性

| 特性 | v1.0 | v2.0 |
|------|------|------|
| 已注册接口 | 13个 | 18个 |
| 带参数通用代理 | ❌ | ✅ 支持任意参数 |
| TTL 缓存 | ❌ | ✅ 分级缓存60s~1h |
| 请求限流 | ❌ | ✅ 自动随机延迟 |
| 请求统计 | ❌ | ✅ /stats 实时监控 |
| 缓存管理 | ❌ | ✅ POST /cache/clear |
| 白名单保护 | ❌ | ✅ 只允许 stock_/bond_ 等前缀 |

## 快速启动

```bash
# Docker 部署
docker-compose up -d --build

# 本地开发
pip install -r requirements.txt
python app.py
```

## 接口列表

### 基础接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查（含缓存统计） |
| `/stats` | GET | 请求统计 & 监控 |
| `/cache/clear` | POST | 清除所有缓存 |

### A股行情

| 接口 | 参数 | 缓存TTL | 说明 |
|------|------|---------|------|
| `/api/stock/zh_a_spot_em` | 无 | 60s | A股全市场实时行情 |
| `/api/stock/zh_a_hist` | symbol,period,start_date,end_date,adjust | 5min | 个股历史K线 |
| `/api/stock/a_indicator_lg` | symbol | 5min | PE/PB历史估值指标 |
| `/api/stock/individual_info_em` | symbol | 5min | 个股详细信息 |

### 港股行情

| 接口 | 参数 | 缓存TTL | 说明 |
|------|------|---------|------|
| `/api/stock/hk_spot_em` | 无 | 60s | 港股全市场实时行情 |

### 资金流向

| 接口 | 参数 | 缓存TTL | 说明 |
|------|------|---------|------|
| `/api/stock/hsgt_hold_stock_em` | market,indicator | 3min | 北向资金持仓排名 |
| `/api/stock/individual_fund_flow_rank` | indicator | 3min | 个股资金流向排名 |

### 板块 & 涨跌停

| 接口 | 参数 | 缓存TTL | 说明 |
|------|------|---------|------|
| `/api/stock/board_concept_name_em` | 无 | 10min | 东财概念板块列表 |
| `/api/stock/board_concept_cons_em` | symbol | 10min | 东财概念板块成份股 |
| `/api/stock/board_concept_name_ths` | 无 | 10min | 同花顺概念板块列表 |
| `/api/stock/board_concept_cons_ths` | symbol | 10min | 同花顺概念板块成份股 |
| `/api/stock/zt_pool_em` | date | 2min | 涨停股池 |
| `/api/stock/zt_pool_dtgc_em` | date | 2min | 跌停股池 |

### 快讯 & 其他

| 接口 | 参数 | 缓存TTL | 说明 |
|------|------|---------|------|
| `/api/stock/info_global_em` | 无 | 2min | 东财全球快讯 |
| `/api/stock/info_global_sina` | 无 | 2min | 新浪全球快讯 |
| `/api/stock/info_global_cls` | 无 | 2min | 财联社快讯 |
| `/api/stock/info_a_code_name` | 无 | 1h | A股代码名称 |
| `/api/stock/gdfx_top_10_em` | symbol,date | 5min | 十大股东 |
| `/api/stock/ipo_declare_em` | 无 | 10min | IPO申报 |

### 通用代理（万能接口）

```
GET /api/akshare/{func_name}?param1=val1&param2=val2
```

支持调用任意 akshare 函数，参数通过 query string 传递。
受白名单保护，只允许 `stock_`、`bond_`、`fund_`、`index_`、`macro_` 等前缀。

**示例：**
```bash
# 个股PE/PB指标
curl "http://gateway:9898/api/akshare/stock_a_indicator_lg?symbol=600519"

# 沪深300指数PE
curl "http://gateway:9898/api/akshare/stock_a_indicator_lg?symbol=000300"

# 个股历史K线
curl "http://gateway:9898/api/akshare/stock_zh_a_hist?symbol=600519&period=daily"
```

## 配置 ai-stock 连接

在 `ai-stock/.env` 中添加：
```bash
AKSHARE_GATEWAY_URL=http://akshare-gateway:9898
```

如果 ai-stock 和 gateway 在同一台服务器上但不同的 Docker 网络中：
```bash
# 方式1: 使用宿主机IP
AKSHARE_GATEWAY_URL=http://172.17.0.1:9898

# 方式2: 使用 Docker 网络
# 在 docker-compose.yml 中让两个服务共享网络
```

## 测试

```bash
# 测试本地
python test_gateway.py

# 测试远程
python test_gateway.py http://your-server:9898
```

## 缓存策略

| 数据类型 | TTL | 说明 |
|---------|-----|------|
| 实时行情 | 60s | 交易时间内频繁变化 |
| 涨跌停池 | 2min | 盘中实时更新 |
| 资金流向 | 3min | 中等频率变化 |
| 个股指标 | 5min | PE/PB分位不常变 |
| 板块列表 | 10min | 很少变化 |
| 代码名称 | 1h | 极少变化 |

> 可通过 `POST /cache/clear` 手动清除缓存
