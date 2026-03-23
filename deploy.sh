#!/bin/bash
# ============================================================
# AKShare Gateway — 阿里云 ECS 一键部署脚本
# ============================================================
#
# 用法:
#   chmod +x deploy.sh && ./deploy.sh
#
# 支持模式:
#   ./deploy.sh              # 完整部署（安装依赖 + 拉代码 + 启动服务）
#   ./deploy.sh update       # 更新部署（拉最新代码 + 重启服务）
#   ./deploy.sh restart      # 仅重启服务
#   ./deploy.sh stop         # 停止服务
#   ./deploy.sh status       # 查看服务状态
#   ./deploy.sh logs         # 查看实时日志
#   ./deploy.sh test         # 运行接口测试
#
# 环境要求:
#   - CentOS 7/8, Ubuntu 20.04+, Alibaba Cloud Linux
#   - Python 3.10+ (脚本会自动安装)
#   - Git (脚本会自动安装)
#
# ============================================================

set -e

# ==================== 配置区 ====================

# 项目 Git 仓库
GIT_REPO="git@github.com:focus-evan/akshare-gateway.git"
GIT_BRANCH="main"

# 部署目录
DEPLOY_DIR="/opt/akshare-gateway"

# 服务配置
SERVICE_NAME="akshare-gateway"
PORT=9898
WORKERS=2
TIMEOUT=120

# Python 虚拟环境
VENV_DIR="${DEPLOY_DIR}/venv"

# 日志目录
LOG_DIR="/var/log/${SERVICE_NAME}"
PID_FILE="/var/run/${SERVICE_NAME}.pid"

# 清华 PyPI 镜像（国内加速）
PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"

# ==================== 颜色输出 ====================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_section() {
    echo ""
    echo -e "${BOLD}${CYAN}============================================================${NC}"
    echo -e "${BOLD}${CYAN}  $1${NC}"
    echo -e "${BOLD}${CYAN}============================================================${NC}"
    echo ""
}

# ==================== 系统依赖安装 ====================

install_system_deps() {
    log_section "安装系统依赖"

    # 检测包管理器
    if command -v apt-get &>/dev/null; then
        PKG_MGR="apt"
    elif command -v dnf &>/dev/null; then
        PKG_MGR="dnf"
    elif command -v yum &>/dev/null; then
        PKG_MGR="yum"
    else
        log_error "不支持的包管理器，请手动安装 Python 3.8+ 和 Git"
        exit 1
    fi

    log_info "包管理器: ${PKG_MGR}"
    log_info "系统: $(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"' || uname -a)"

    # 安装基础工具
    if [ "$PKG_MGR" = "apt" ]; then
        apt-get update -y
        apt-get install -y python3 python3-pip python3-venv git curl
    elif [ "$PKG_MGR" = "dnf" ]; then
        dnf install -y python3 python3-pip python3-devel git curl gcc
    elif [ "$PKG_MGR" = "yum" ]; then
        yum install -y python3 python3-pip python3-devel git curl gcc
    fi

    # 检测当前 Python 版本
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
    PYTHON_CMD="python3"

    log_info "当前 Python: ${PYTHON_VERSION}"

    # Python 3.8+ 即可运行（requirements.txt 已放宽版本限制）
    if [ "$PYTHON_MAJOR" -ge 3 ] && [ "$PYTHON_MINOR" -ge 8 ]; then
        log_info "Python ${PYTHON_VERSION} 满足要求 ✅"
    else
        log_warn "Python ${PYTHON_VERSION} 版本过低（需要 3.8+），尝试安装更新版本..."

        if [ "$PKG_MGR" = "apt" ]; then
            # Ubuntu/Debian — 通过 deadsnakes PPA
            apt-get install -y software-properties-common
            add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
            apt-get update -y
            apt-get install -y python3.11 python3.11-venv python3.11-distutils 2>/dev/null || \
            apt-get install -y python3.10 python3.10-venv python3.10-distutils 2>/dev/null || true

        elif [ "$PKG_MGR" = "dnf" ] || [ "$PKG_MGR" = "yum" ]; then
            # CentOS/RHEL/Alibaba Cloud Linux — 尝试多种方式
            # 方法1: 从 AppStream/EPEL 安装
            ${PKG_MGR} install -y python3.11 python3.11-pip python3.11-devel 2>/dev/null || \
            ${PKG_MGR} install -y python3.10 python3.10-pip python3.10-devel 2>/dev/null || \
            ${PKG_MGR} install -y python39 python39-pip python39-devel 2>/dev/null || true

            # 方法2: 如果上面没装上，尝试启用 CRB/PowerTools
            if ! command -v python3.11 &>/dev/null && ! command -v python3.10 &>/dev/null; then
                ${PKG_MGR} install -y epel-release 2>/dev/null || true
                ${PKG_MGR} install -y python3.11 2>/dev/null || \
                ${PKG_MGR} install -y python3.10 2>/dev/null || true
            fi
        fi

        # 找到最佳可用的 Python
        for PY in python3.11 python3.10 python3.9 python3; do
            if command -v "$PY" &>/dev/null; then
                PY_VER=$($PY --version 2>&1 | awk '{print $2}')
                PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
                if [ "$PY_MINOR" -ge 8 ]; then
                    PYTHON_CMD="$PY"
                    log_info "使用 ${PY} (${PY_VER})"
                    break
                fi
            fi
        done
    fi

    # 确保 venv 模块可用
    ${PYTHON_CMD} -m venv --help &>/dev/null 2>&1 || {
        log_warn "venv 模块不可用，尝试安装..."
        if [ "$PKG_MGR" = "apt" ]; then
            apt-get install -y python3-venv 2>/dev/null || true
        elif [ "$PKG_MGR" = "dnf" ] || [ "$PKG_MGR" = "yum" ]; then
            ${PKG_MGR} install -y python3-virtualenv 2>/dev/null || true
        fi
    }

    # 最终验证
    FINAL_VERSION=$(${PYTHON_CMD} --version 2>&1)
    log_info "最终 Python: ${FINAL_VERSION}"
    log_info "Git: $(git --version)"
}

# ==================== 项目部署 ====================

deploy_project() {
    log_section "部署项目代码"

    # 创建必要目录
    mkdir -p "${DEPLOY_DIR}"
    mkdir -p "${LOG_DIR}"

    if [ -d "${DEPLOY_DIR}/.git" ]; then
        log_info "项目已存在，拉取最新代码..."
        cd "${DEPLOY_DIR}"
        git fetch origin
        git reset --hard "origin/${GIT_BRANCH}"
        git pull origin "${GIT_BRANCH}"
    else
        log_info "克隆项目..."
        git clone -b "${GIT_BRANCH}" "${GIT_REPO}" "${DEPLOY_DIR}"
    fi

    cd "${DEPLOY_DIR}"
    COMMIT=$(git log -1 --format='%h %s' 2>/dev/null || echo "unknown")
    log_info "当前版本: ${COMMIT}"
}

# ==================== Python 环境 ====================

setup_python_env() {
    log_section "配置 Python 虚拟环境"

    cd "${DEPLOY_DIR}"

    if [ ! -d "${VENV_DIR}" ]; then
        log_info "创建虚拟环境..."
        ${PYTHON_CMD:-python3} -m venv "${VENV_DIR}"
    fi

    # 激活虚拟环境
    source "${VENV_DIR}/bin/activate"

    log_info "升级 pip..."
    pip install --upgrade pip -i "${PIP_INDEX}" -q

    log_info "安装依赖..."
    pip install -r requirements.txt -i "${PIP_INDEX}"

    log_info "Python: $(python --version)"
    log_info "akshare: $(python -c 'import akshare; print(akshare.__version__)' 2>/dev/null || echo 'installing...')"
    log_info "依赖安装完成 ✅"
}

# ==================== Systemd 服务 ====================

create_systemd_service() {
    log_section "配置 systemd 服务"

    cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=AKShare Gateway — 第三方数据接口网关
Documentation=https://github.com/focus-evan/akshare-gateway
After=network.target
Wants=network-online.target

[Service]
Type=notify
User=root
Group=root
WorkingDirectory=${DEPLOY_DIR}
ExecStart=${VENV_DIR}/bin/gunicorn app:app \\
    --bind 0.0.0.0:${PORT} \\
    --workers ${WORKERS} \\
    --worker-class uvicorn.workers.UvicornWorker \\
    --timeout ${TIMEOUT} \\
    --graceful-timeout 30 \\
    --keep-alive 5 \\
    --access-logfile ${LOG_DIR}/access.log \\
    --error-logfile ${LOG_DIR}/error.log \\
    --capture-output \\
    --enable-stdio-inheritance

ExecReload=/bin/kill -s HUP \$MAINPID
ExecStop=/bin/kill -s TERM \$MAINPID

# 环境变量
Environment="TZ=Asia/Shanghai"
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONDONTWRITEBYTECODE=1"

# 自动重启
Restart=always
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=60

# 资源限制
LimitNOFILE=65536
MemoryMax=2G
CPUQuota=200%

# 日志配置
StandardOutput=append:${LOG_DIR}/stdout.log
StandardError=append:${LOG_DIR}/stderr.log

[Install]
WantedBy=multi-user.target
EOF

    # 日志轮转配置
    cat > "/etc/logrotate.d/${SERVICE_NAME}" << EOF
${LOG_DIR}/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    copytruncate
    maxsize 100M
}
EOF

    systemctl daemon-reload
    log_info "systemd 服务配置完成 ✅"
}

# ==================== 防火墙 ====================

configure_firewall() {
    log_section "配置防火墙"

    # 检测防火墙类型
    if command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld; then
        log_info "检测到 firewalld，开放端口 ${PORT}..."
        firewall-cmd --permanent --add-port=${PORT}/tcp 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
        log_info "firewalld 端口 ${PORT} 已开放 ✅"
    elif command -v ufw &>/dev/null; then
        log_info "检测到 ufw，开放端口 ${PORT}..."
        ufw allow ${PORT}/tcp 2>/dev/null || true
        log_info "ufw 端口 ${PORT} 已开放 ✅"
    elif command -v iptables &>/dev/null; then
        log_info "使用 iptables 开放端口 ${PORT}..."
        iptables -I INPUT -p tcp --dport ${PORT} -j ACCEPT 2>/dev/null || true
        log_info "iptables 端口 ${PORT} 已开放 ✅"
    else
        log_warn "未检测到防火墙，跳过"
    fi

    echo ""
    log_warn "⚠️  请确保阿里云安全组也已开放端口 ${PORT}"
    log_warn "   入口: ECS控制台 → 安全组 → 入方向 → 添加规则"
    log_warn "   协议: TCP, 端口: ${PORT}, 授权对象: ai-stock服务器IP"
}

# ==================== 服务管理 ====================

start_service() {
    log_section "启动服务"
    systemctl enable "${SERVICE_NAME}"
    systemctl start "${SERVICE_NAME}"
    sleep 2

    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        log_info "服务启动成功 ✅"
        log_info "端口: ${PORT}"
        log_info "状态: $(systemctl is-active ${SERVICE_NAME})"

        # 健康检查
        sleep 3
        if curl -sf "http://localhost:${PORT}/health" > /dev/null 2>&1; then
            HEALTH=$(curl -s "http://localhost:${PORT}/health")
            log_info "健康检查通过 ✅"
            echo "  ${HEALTH}"
        else
            log_warn "健康检查暂时未通过，服务可能还在初始化..."
        fi
    else
        log_error "服务启动失败 ❌"
        log_error "请检查日志: journalctl -u ${SERVICE_NAME} -n 50"
        exit 1
    fi
}

stop_service() {
    log_info "停止服务..."
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    log_info "服务已停止"
}

restart_service() {
    log_section "重启服务"
    systemctl restart "${SERVICE_NAME}"
    sleep 3

    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        log_info "服务重启成功 ✅"
        # 健康检查
        sleep 2
        HEALTH=$(curl -s "http://localhost:${PORT}/health" 2>/dev/null || echo '{"status":"starting"}')
        echo "  ${HEALTH}"
    else
        log_error "服务重启失败 ❌"
        journalctl -u "${SERVICE_NAME}" -n 20 --no-pager
        exit 1
    fi
}

show_status() {
    echo ""
    echo -e "${BOLD}服务状态:${NC}"
    systemctl status "${SERVICE_NAME}" --no-pager -l 2>/dev/null || echo "服务未安装"
    echo ""

    if curl -sf "http://localhost:${PORT}/health" > /dev/null 2>&1; then
        echo -e "${BOLD}健康检查:${NC}"
        curl -s "http://localhost:${PORT}/health" | python3 -m json.tool 2>/dev/null || curl -s "http://localhost:${PORT}/health"
        echo ""
        echo -e "${BOLD}统计信息:${NC}"
        curl -s "http://localhost:${PORT}/stats" | python3 -m json.tool 2>/dev/null || curl -s "http://localhost:${PORT}/stats"
    else
        echo -e "${RED}服务未响应${NC}"
    fi
}

show_logs() {
    journalctl -u "${SERVICE_NAME}" -f --no-pager
}

run_test() {
    log_section "运行接口测试"
    cd "${DEPLOY_DIR}"
    source "${VENV_DIR}/bin/activate"

    if [ -f "test_gateway.py" ]; then
        python test_gateway.py --mode all --gateway "http://localhost:${PORT}" "$@"
    else
        log_error "test_gateway.py 未找到"
        exit 1
    fi
}

# ==================== 部署后信息 ====================

print_summary() {
    log_section "部署完成 🎉"

    # 获取服务器 IP
    PUBLIC_IP=$(curl -s --connect-timeout 3 http://ifconfig.me 2>/dev/null || \
                curl -s --connect-timeout 3 http://icanhazip.com 2>/dev/null || \
                hostname -I | awk '{print $1}')

    echo -e "  ${BOLD}服务地址:${NC}  http://${PUBLIC_IP}:${PORT}"
    echo -e "  ${BOLD}健康检查:${NC}  http://${PUBLIC_IP}:${PORT}/health"
    echo -e "  ${BOLD}统计信息:${NC}  http://${PUBLIC_IP}:${PORT}/stats"
    echo -e "  ${BOLD}接口文档:${NC}  http://${PUBLIC_IP}:${PORT}/docs"
    echo ""
    echo -e "  ${BOLD}常用命令:${NC}"
    echo "    systemctl status ${SERVICE_NAME}      # 查看状态"
    echo "    systemctl restart ${SERVICE_NAME}     # 重启服务"
    echo "    systemctl stop ${SERVICE_NAME}        # 停止服务"
    echo "    journalctl -u ${SERVICE_NAME} -f      # 查看实时日志"
    echo "    tail -f ${LOG_DIR}/access.log         # 查看访问日志"
    echo ""
    echo -e "  ${BOLD}ai-stock 配置:${NC}"
    echo "    在 ai-stock 的 .env 中设置:"
    echo -e "    ${CYAN}AKSHARE_GATEWAY_URL=http://${PUBLIC_IP}:${PORT}${NC}"
    echo ""
    echo -e "  ${BOLD}接口测试:${NC}"
    echo "    cd ${DEPLOY_DIR} && ./deploy.sh test"
    echo ""
}

# ==================== 主入口 ====================

main() {
    ACTION="${1:-deploy}"

    case "$ACTION" in
        deploy)
            log_section "AKShare Gateway 一键部署"
            echo "  模式: 完整部署"
            echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"

            install_system_deps
            deploy_project
            setup_python_env
            create_systemd_service
            configure_firewall
            start_service
            print_summary
            ;;

        update)
            log_section "AKShare Gateway 更新部署"
            PYTHON_CMD="${VENV_DIR}/bin/python"
            deploy_project
            source "${VENV_DIR}/bin/activate"
            pip install -r requirements.txt -i "${PIP_INDEX}" -q
            restart_service
            print_summary
            ;;

        restart)
            restart_service
            ;;

        stop)
            stop_service
            ;;

        status)
            show_status
            ;;

        logs)
            show_logs
            ;;

        test)
            shift
            run_test "$@"
            ;;

        *)
            echo "用法: $0 {deploy|update|restart|stop|status|logs|test}"
            echo ""
            echo "  deploy   - 完整部署（首次使用）"
            echo "  update   - 更新部署（拉代码+重启）"
            echo "  restart  - 重启服务"
            echo "  stop     - 停止服务"
            echo "  status   - 查看服务状态"
            echo "  logs     - 查看实时日志"
            echo "  test     - 运行接口测试"
            exit 1
            ;;
    esac
}

main "$@"
