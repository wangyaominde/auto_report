#!/usr/bin/env bash

# WorkLog 一键启动脚本
# - 自动检测可用的 Python 运行环境
# - 自动检测关键依赖和平台命令
# - 自动检测并提示配置 LLM_API_KEY
# - 支持透传参数启动主脚本（如 --report）

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="${PROJECT_ROOT}/claude_auto_report_code_minimax.py"
ENV_FILE="${PROJECT_ROOT}/.env"
VENV_DIR="${PROJECT_ROOT}/venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"

log_info() { echo "[INFO] $*"; }
log_warn() { echo "[WARN] $*"; }
log_error() { echo "[ERROR] $*" >&2; }

if [ ! -f "$SCRIPT_PATH" ]; then
  log_error "未找到主脚本: $SCRIPT_PATH"
  exit 1
fi

detect_base_python() {
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    echo "python"
    return 0
  fi

  return 1
}

BASE_PYTHON="$(detect_base_python || true)"
if [ -z "$BASE_PYTHON" ]; then
  log_error "未检测到可用 Python（python3/python），请先安装 Python 3.9+"
  exit 1
fi

ensure_venv() {
  if [ -x "$VENV_PYTHON" ]; then
    echo "$VENV_PYTHON"
    return 0
  fi

  log_warn "未检测到可用 venv 环境，开始创建 ${VENV_DIR} ..."
  "$BASE_PYTHON" -m venv "$VENV_DIR"

  if [ ! -x "$VENV_PYTHON" ]; then
    log_error "venv 创建失败：${VENV_PYTHON} 不存在"
    return 1
  fi

  echo "$VENV_PYTHON"
}

PYTHON_BIN="$(ensure_venv || true)"
if [ -z "$PYTHON_BIN" ]; then
  log_error "Python 环境准备失败"
  exit 1
fi

python_has_module() {
  local module="$1"
  "$PYTHON_BIN" - "$module" <<'PY'
import importlib.util
import sys
sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)
PY
}

install_windows_modules() {
  local modules=("psutil" "pywin32")
  local to_install=()
  for module in "${modules[@]}"; do
    if ! python_has_module "$module"; then
      to_install+=("$module")
    fi
  done

  if [ ${#to_install[@]} -eq 0 ]; then
    return 0
  fi

  log_info "检测到缺失 Windows 依赖：${to_install[*]}"
  if ! "$PYTHON_BIN" -m pip install "${to_install[@]}"; then
    log_error "Windows 依赖安装失败：${to_install[*]}"
    return 1
  fi
}

PYTHON_VER="$($PYTHON_BIN --version 2>&1 | sed 's/Python //')"
log_info "检测到 Python: $PYTHON_BIN ($PYTHON_VER)"

log_info "检查并更新依赖..."
"$PYTHON_BIN" -m pip install --upgrade pip >/dev/null
if [ -f "$PROJECT_ROOT/requirements.txt" ]; then
  "$PYTHON_BIN" -m pip install -r "$PROJECT_ROOT/requirements.txt" --upgrade >/dev/null
fi

log_info "检测系统与采集命令..."
OS_NAME="$(uname -s)"
case "$OS_NAME" in
  Darwin)
    if ! command -v osascript >/dev/null 2>&1; then
      log_error "macOS 上未检测到 osascript，无法采集前台窗口"
      exit 1
    fi
    ;;
  Linux)
    if ! command -v xdotool >/dev/null 2>&1; then
      log_warn "Linux 上未检测到 xdotool，窗口采集功能可能不可用"
    fi
    ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    log_warn "Windows 平台检测到，将使用 win32 API 采集窗口（需 pywin32 与 psutil）"
    if ! install_windows_modules; then
      log_error "Windows 采集依赖未就绪，已中止。"
      exit 1
    fi
    ;;
  *)
    log_warn "未知平台: $OS_NAME，脚本将直接尝试运行，部分采集能力可能受限"
    ;;
esac

if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

if [ -z "${LLM_API_KEY:-}" ] && [ -n "${MINIMAX_API_KEY:-}" ]; then
  export LLM_API_KEY="$MINIMAX_API_KEY"
fi
if [ -z "${MINIMAX_API_KEY:-}" ] && [ -n "${LLM_API_KEY:-}" ]; then
  export MINIMAX_API_KEY="$LLM_API_KEY"
fi

if [ -z "${LLM_API_KEY:-}" ]; then
  log_warn "当前环境未检测到 LLM_API_KEY（未检测到 MINIMAX_API_KEY 兼容值）"
  read -r -p "请输入 LLM API Key（空则退出）: " INPUT_KEY
  if [ -z "$INPUT_KEY" ]; then
    log_error "LLM_API_KEY 为空，无法启动"
    exit 1
  fi
  export LLM_API_KEY="$INPUT_KEY"
  export MINIMAX_API_KEY="$INPUT_KEY"
  if [ ! -f "$ENV_FILE" ]; then
    log_info "检测到未存在 .env，已创建并写入当前会话密钥（可按需清理）"
    {
      printf "LLM_API_KEY=%s\n" "$INPUT_KEY"
      printf "MINIMAX_API_KEY=%s\n" "$INPUT_KEY"
    } > "$ENV_FILE"
  fi
fi

log_info "启动 WorkLog..."
export LLM_API_KEY
export MINIMAX_API_KEY
exec "$PYTHON_BIN" "$SCRIPT_PATH" "$@"
