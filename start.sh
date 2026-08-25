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
  ./start.sh [--https|--http] [--port <port>] [--install] [--keep-port]

参数：
  --https           使用 HTTPS 启动（默认）
  --http            使用 HTTP 启动
  --port <port>     指定端口（默认：读取 config/config.json 或 15677）
  --install         启动前安装 requirements.txt（用选定的解释器 -m pip）
  --keep-port       端口被占用时不自动清理，直接报错退出（默认自动关闭占用端口的进程）

示例：
  ./start.sh
  ./start.sh --http --port 15677
EOF
}

USE_HTTPS=1
PORT=""
DO_INSTALL=0
KEEP_PORT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --https) USE_HTTPS=1; shift ;;
    --http) USE_HTTPS=0; shift ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --install) DO_INSTALL=1; shift ;;
    --keep-port) KEEP_PORT=1; shift ;;
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


# 选择 Python 解释器：优先项目自带的 .doubao 虚拟环境（依赖已装好且版本匹配），
# 其次 .venv，最后回退系统 python3
if [[ -x ".doubao/bin/python" ]]; then
  PY="./.doubao/bin/python"
elif [[ -x ".venv/bin/python" ]]; then
  PY="./.venv/bin/python"
else
  PY="python3"
fi
echo "使用解释器：$PY"

# 可选：安装依赖（避免走错 pip，使用解释器自带的 -m pip）
if [[ "$DO_INSTALL" -eq 1 ]]; then
  echo "安装 Python 依赖..."
  "$PY" -m pip install -r requirements.txt
fi

# 端口：优先使用 --port，其次尝试从 config/config.json 读取 server.port，否则 15677
if [[ -z "$PORT" ]]; then
  if command -v "$PY" >/dev/null 2>&1 || [[ "$PY" == ./* ]]; then
    PORT="$("$PY" - <<'PYEOF'
import json
try:
    with open("config/config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    print(int(cfg.get("server", {}).get("port", 15677)))
except Exception:
    print(15677)
PYEOF
)"
  else
    PORT="15677"
  fi
fi

echo "启动服务：start_server.py 端口=$PORT 协议=$([[ "$USE_HTTPS" -eq 1 ]] && echo https || echo http)"

# 端口占用检查：默认自动关闭占用端口的进程后启动（--keep-port 可关闭该行为）
port_pids() {
  # 只匹配 LISTEN 状态，避免误杀恰好连到该端口的客户端
  lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null || true
}

if command -v lsof >/dev/null 2>&1; then
  OCCUPIED="$(port_pids)"
  if [ -n "$OCCUPIED" ]; then
    if [[ "$KEEP_PORT" -eq 1 ]]; then
      echo "错误: 端口 $PORT 已被占用（PID: $(echo $OCCUPIED | tr '\n' ' ')），且指定了 --keep-port 不自动清理"
      exit 1
    fi
    echo "端口 $PORT 被占用（PID: $(echo $OCCUPIED | tr '\n' ' ')），自动关闭..."
    kill $OCCUPIED 2>/dev/null || true
    # 最多等 15 秒优雅退出，超时强杀
    for _ in $(seq 1 15); do
      [ -z "$(port_pids)" ] && break
      sleep 1
    done
    REMAIN="$(port_pids)"
    if [ -n "$REMAIN" ]; then
      echo "优雅关闭超时，强制结束（kill -9）..."
      kill -9 $REMAIN 2>/dev/null || true
      sleep 1
    fi
    # 再兜底等端口完全释放（TIME_WAIT/内核回收），最多 10 秒
    for _ in $(seq 1 10); do
      [ -z "$(port_pids)" ] && break
      sleep 1
    done
    echo "端口 $PORT 已释放，继续启动"
  fi
else
  echo "警告: 未安装 lsof，跳过端口占用自动清理（brew install lsof / apt install lsof 可启用）"
fi

ARGS=( "./start_server.py" "--port" "$PORT" )
if [[ "$USE_HTTPS" -eq 1 ]]; then
  ARGS+=( "--https" )
fi

"$PY" "${ARGS[@]}"

