# AKShare Gateway

将 [AKShare](https://github.com/akfamily/akshare) 封装为独立 HTTP API 微服务，部署在 Docker 容器中，供 `ai-stock` 主服务通过内网调用。

## 为什么需要这个项目？

> **问题**：阿里云 ECS 上直接通过 akshare 调用东方财富 API 时，频繁报 `RemoteDisconnected` 错误。  
> **原因**：东方财富的反爬系统针对云服务器 IP 进行封锁，无论怎么换 UA 都无效。  
> **方案**：将 akshare 独立部署为微服务，通过 Docker 内网通信，主服务调用网关而非直接调用 akshare。

### 架构图

```
┌─────────────────────────────────────────────────┐
│              Docker 网络 (s-ai-agent-net)          │
│                                                   │
│  ┌──────────────┐     HTTP/9898    ┌────────────┐ │
│  │  ai-stock    │ ───────────────> │  akshare   │ │
│  │  (端口 8000) │    内网通信       │  gateway   │ │
│  │              │ <─────────────── │  (端口9898)│ │
│  └──────────────┘     JSON         └─────┬──────┘ │
│                                          │        │
└──────────────────────────────────────────┼────────┘
                                           │
                                      akshare 库
                                           │
                              ┌────────────┼────────────┐
                              │            │            │
                          东方财富      新浪财经      同花顺
```

## 快速开始

### 1. 本地开发测试

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
python app.py
# 服务运行在 http://localhost:9898

# 运行测试
python test_gateway.py
```

### 2. Docker 部署

```bash
# 构建镜像
docker build -t akshare-gateway:latest .

# 启动
docker-compose up -d

# 查看日志
docker-compose logs -f

# 测试
python test_gateway.py http://localhost:9898
```

## API 接口文档

启动后访问 **http://localhost:9898/docs** 查看 Swagger 交互式文档。

### 接口列表

| 接口 | 方法 | 对应 akshare 函数 | 参数 |
|------|------|-------------------|------|
| `/health` | GET | — | — |
| `/api/stock/zh_a_spot_em` | GET | `stock_zh_a_spot_em()` | — |
| `/api/stock/zh_a_hist` | GET | `stock_zh_a_hist()` | symbol, period, start_date, end_date, adjust |
| `/api/stock/zt_pool_em` | GET | `stock_zt_pool_em()` | date |
| `/api/stock/zt_pool_dtgc_em` | GET | `stock_zt_pool_dtgc_em()` | date |
| `/api/stock/board_concept_name_em` | GET | `stock_board_concept_name_em()` | — |
| `/api/stock/board_concept_cons_em` | GET | `stock_board_concept_cons_em()` | symbol |
| `/api/stock/board_concept_name_ths` | GET | `stock_board_concept_name_ths()` | — |
| `/api/stock/board_concept_cons_ths` | GET | `stock_board_concept_cons_ths()` | symbol |
| `/api/stock/info_global_em` | GET | `stock_info_global_em()` | — |
| `/api/stock/info_global_sina` | GET | `stock_info_global_sina()` | — |
| `/api/stock/info_global_cls` | GET | `stock_info_global_cls()` | — |
| `/api/stock/info_a_code_name` | GET | `stock_info_a_code_name()` | — |
| `/api/stock/gdfx_top_10_em` | GET | `stock_gdfx_top_10_em()` | symbol, date |
| `/api/stock/ipo_declare_em` | GET | `stock_ipo_declare_em()` | — |
| `/api/stock/hk_ipo_wait_board_em` | GET | `stock_hk_ipo_wait_board_em()` | — |
| `/api/stock/hk_spot_em` | GET | `stock_hk_spot_em()` | — |
| `/api/akshare/{func_name}` | GET | 通用代理（仅限无参数函数） | — |

### 响应格式

所有接口返回统一的 JSON 格式：

```json
{
  "status": "ok",
  "count": 5280,
  "data": [
    {"代码": "000001", "名称": "平安银行", "最新价": 12.5, ...},
    ...
  ]
}
```

### 示例请求

```bash
# A股实时行情
curl http://localhost:9898/api/stock/zh_a_spot_em

# 个股历史K线
curl "http://localhost:9898/api/stock/zh_a_hist?symbol=600519&period=daily&start_date=20241201&end_date=20241231&adjust=qfq"

# 涨停股池
curl "http://localhost:9898/api/stock/zt_pool_em?date=20241220"

# 概念板块成份股
curl "http://localhost:9898/api/stock/board_concept_cons_em?symbol=人工智能"
```

## 阿里云部署指南

### 前置条件

- 阿里云 ECS 已安装 Docker + Docker Compose
- `ai-stock` 项目已部署并使用 `s-ai-agent-net` 网络

### 部署步骤

#### 1. 上传代码到服务器

```bash
# 方式一：通过 Git
cd /data
git clone git@github.com:focus-evan/akshare-gateway.git
cd akshare-gateway

# 方式二：通过 scp
scp -r ./akshare-gateway root@your-server:/data/
```

#### 2. 确保 Docker 网络存在

```bash
# 检查网络是否存在
docker network ls | grep s-ai-agent-net

# 如果不存在，先启动 ai-stock 创建网络
# 或手动创建
docker network create s-ai-agent-net
```

#### 3. 构建并启动

```bash
cd /data/akshare-gateway

# 构建镜像
docker-compose build

# 启动服务
docker-compose up -d

# 查看状态
docker-compose ps

# 查看日志
docker-compose logs -f akshare-gateway
```

#### 4. 验证服务

```bash
# 健康检查
curl http://localhost:9898/health

# 测试核心接口
curl http://localhost:9898/api/stock/zh_a_spot_em | python3 -m json.tool | head -20

# 运行完整测试
python3 test_gateway.py http://localhost:9898
```

#### 5. 配置 ai-stock 使用网关

在 `ai-stock` 项目的 `.env` 文件中添加：

```env
AKSHARE_GATEWAY_URL=http://akshare-gateway:9898
```

或在 `docker-compose.yml` 中为 api 服务添加环境变量：

```yaml
services:
  api:
    environment:
      AKSHARE_GATEWAY_URL: http://akshare-gateway:9898
```

#### 6. 重启 ai-stock

```bash
cd /data/ai-stock
docker-compose up -d
```

### ai-stock 接入方式

`ai-stock` 中已创建 `api/akshare_client.py` 客户端模块。在策略文件中只需将：

```python
# 旧方式
import akshare as ak
df = ak.stock_zh_a_spot_em()
```

替换为：

```python
# 新方式 — 通过网关调用
from akshare_client import ak_client as ak
df = ak.stock_zh_a_spot_em()  # 接口名称完全一致，无需改其他代码
```

### 运维

```bash
# 查看日志
docker-compose logs -f --tail 100

# 重启服务
docker-compose restart

# 更新代码后重新构建
git pull
docker-compose up -d --build

# 查看资源占用
docker stats akshare-gateway
```

## 开发

```bash
# 本地开发
pip install -r requirements.txt
python app.py

# 访问 Swagger 文档
open http://localhost:9898/docs
```

## 技术栈

- **Python 3.11**
- **FastAPI** — 异步 Web 框架
- **AKShare** — A股数据获取库
- **Gunicorn + Uvicorn** — 生产级 ASGI 服务器
- **Docker** — 容器化部署

## License

MIT
