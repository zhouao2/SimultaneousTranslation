#!/bin/bash

# 同声传译系统启动脚本（Linux/macOS）
# - 默认启动 start_server.py（HTTP/HTTPS + WebSocket）
# - 可选安装依赖（requirements.txt）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

usage() {
  cat <<'EOF'
用法：
  ./start.sh [--https|--http] [--port <port>] [--install]

参数：
  --https           使用 HTTPS 启动（默认）
  --http            使用 HTTP 启动
  --port <port>     指定端口（默认：读取 config/config.json 或 15677）
  --install         启动前安装 requirements.txt（python3 -m pip）

示例：
  ./start.sh
  ./start.sh --http --port 15677
EOF
}

USE_HTTPS=1
PORT=""
DO_INSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --https) USE_HTTPS=1; shift ;;
    --http) USE_HTTPS=0; shift ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --install) DO_INSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage; exit 1 ;;
  esac
done

echo "启动同声传译系统..."

# 检查配置文件
if [ ! -f "config/config.json" ]; then
    echo "错误: 配置文件不存在！"
    echo "请复制 config/config.example.json 为 config/config.json 并填入配置"
    exit 1
fi


# 可选：安装依赖（避免走错 pip，使用 python3 -m pip）
if [[ "$DO_INSTALL" -eq 1 ]]; then
  echo "安装 Python 依赖..."
  python3 -m pip install -r requirements.txt
fi

# 端口：优先使用 --port，其次尝试从 config/config.json 读取 server.port，否则 15677
if [[ -z "$PORT" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PORT="$(python3 - <<'PY'
import json
try:
    with open("config/config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    print(int(cfg.get("server", {}).get("port", 15677)))
except Exception:
    print(15677)
PY
)"
  else
    PORT="15677"
  fi
fi

echo "启动服务：start_server.py 端口=$PORT 协议=$([[ "$USE_HTTPS" -eq 1 ]] && echo https || echo http)"
ARGS=( "./start_server.py" "--port" "$PORT" )
if [[ "$USE_HTTPS" -eq 1 ]]; then
  ARGS+=( "--https" )
fi

python3 "${ARGS[@]}"

