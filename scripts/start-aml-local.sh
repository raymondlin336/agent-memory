#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Local (no Docker) AML stack for Linux: MiniLM rerank server + AML Add/Search adapter in
# in-memory mode. Same behaviour and settings as scripts/start-aml-local.ps1 on Windows.
#
#   scripts/start-aml-local.sh            # start, wait until healthy
#   scripts/start-aml-local.sh --stop     # stop what it started
#
# Options: --port 8200, --host 127.0.0.1 (0.0.0.0 to listen beyond localhost), --env-file <path>,
# --python <path>. Settings come from deploy/aml/.env (falls back to evaluation/environment/.env);
# variables already set in the shell win over the file. The reranker is scripts/rerank_server.py on
# the port of RERANK_BASE_URL, run with the adapter's python (needs pip install -e ".[rerank]");
# RERANK_SERVER_PYTHON / RERANK_SERVER_SCRIPT override that. If the reranker already answers, it
# is left alone. Logs, pids and the adapter ledger go to evaluation/outputs/aml_local/ (git-ignored).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/evaluation/outputs/aml_local"
PID_FILE="$RUN_DIR/pids"
mkdir -p "$RUN_DIR"

STOP=0 ENV_FILE="" PORT=8200 BIND_HOST="127.0.0.1" PYTHON=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stop) STOP=1; shift ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --host) BIND_HOST="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

kill_tree() {
  local pid="$1"
  if [[ -r "/proc/$pid/winpid" ]]; then  # Git Bash on Windows: native process tree
    taskkill //PID "$(cat "/proc/$pid/winpid")" //T //F >/dev/null 2>&1 || true
  else
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  fi
}

if [[ $STOP -eq 1 ]]; then
  [[ -f "$PID_FILE" ]] || { echo "nothing recorded in $PID_FILE"; exit 0; }
  while read -r name pid; do
    kill_tree "$pid"
    echo "stopped $name (pid $pid)"
  done < "$PID_FILE"
  rm -f "$PID_FILE"
  exit 0
fi

if [[ -z "$ENV_FILE" ]]; then
  ENV_FILE="$ROOT/deploy/aml/.env"
  [[ -f "$ENV_FILE" ]] || ENV_FILE="$ROOT/evaluation/environment/.env"
fi
[[ -f "$ENV_FILE" ]] || { echo "env file not found: $ENV_FILE (copy deploy/aml/.env.example to deploy/aml/.env)" >&2; exit 1; }
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%$'\r'}"
  [[ "$line" =~ ^[[:space:]]*# || "$line" != *=* ]] && continue
  key="$(echo "${line%%=*}" | tr -d '[:space:]')"
  value="${line#*=}"; value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
  [[ -n "${!key:-}" ]] || export "$key=$value"
done < "$ENV_FILE"

case "$BIND_HOST" in 127.0.0.1|localhost|::1) LOOPBACK=1 ;; *) LOOPBACK=0 ;; esac
if [[ -z "${AML_ADAPTER_KEY:-}" ]]; then
  # The dev key is only acceptable on loopback; the adapter refuses weak keys on other binds.
  [[ $LOOPBACK -eq 1 ]] || { echo "AML_ADAPTER_KEY must be set (32+ random characters) to listen on $BIND_HOST" >&2; exit 1; }
  export AML_ADAPTER_KEY="local-dev-key"; echo "AML_ADAPTER_KEY not set; using local-dev-key"
fi
export AML_IN_MEMORY="${AML_IN_MEMORY:-1}"
export AML_STATE_DB="${AML_STATE_DB:-$RUN_DIR/aml_adapter_state.db}"
export PYTHONIOENCODING=utf-8 PYTHONPATH="$ROOT"
if [[ -z "$PYTHON" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
  [[ -x "$PYTHON" ]] || PYTHON="$ROOT/.venv/Scripts/python.exe"
fi
[[ -x "$PYTHON" ]] || { echo "python not found: $PYTHON (create .venv and pip install -e \".[dev,rerank]\")" >&2; exit 1; }

up() { curl -fsS -m 3 -o /dev/null "$1" 2>/dev/null; }

wait_up() {  # name url seconds pid log
  local deadline=$((SECONDS + $3))
  while (( SECONDS < deadline )); do
    if up "$2"; then echo "$1 up: $2"; return 0; fi
    kill -0 "$4" 2>/dev/null || break
    sleep 2
  done
  echo "$1 did not come up; last log lines from $5:"
  tail -n 20 "$5" 2>/dev/null || true
  exit 1
}

start_bg() {  # log cmd... ; prints the pid. setsid gives it its own process group for --stop.
  local log="$1"; shift
  if command -v setsid >/dev/null 2>&1; then
    setsid "$@" >"$log" 2>"$log.err" </dev/null &
  else
    nohup "$@" >"$log" 2>"$log.err" </dev/null &
  fi
  echo $!
}

# Keep the record of a reranker started by an earlier run so --stop still stops it.
OLD_RERANK="$(grep '^reranker ' "$PID_FILE" 2>/dev/null || true)"
: > "$PID_FILE"
export RERANK_BASE_URL="${RERANK_BASE_URL:-http://127.0.0.1:8906/v1}"
export RERANK_API_KEY="${RERANK_API_KEY:-local}"
RERANK_URL="${RERANK_BASE_URL%/}"
if up "$RERANK_URL"; then
  echo "reranker already up: $RERANK_URL"
  [[ -n "$OLD_RERANK" ]] && echo "$OLD_RERANK" >> "$PID_FILE"
else
  RERANK_PORT="$(echo "$RERANK_URL" | sed -E 's#^[a-z]+://[^/:]+:([0-9]+).*#\1#')"
  [[ "$RERANK_PORT" =~ ^[0-9]+$ ]] || RERANK_PORT=8906
  LOG="$RUN_DIR/reranker.log"
  PID="$(start_bg "$LOG" "${RERANK_SERVER_PYTHON:-$PYTHON}" "${RERANK_SERVER_SCRIPT:-$ROOT/scripts/rerank_server.py}" --port "$RERANK_PORT")"
  echo "reranker $PID" >> "$PID_FILE"
  # First start downloads the model (~90 MB), hence the long wait.
  wait_up reranker "$RERANK_URL" 600 "$PID" "$LOG.err"
fi

CHECK_HOST="$BIND_HOST"; [[ "$CHECK_HOST" == "0.0.0.0" || "$CHECK_HOST" == "::" ]] && CHECK_HOST=127.0.0.1
ADAPTER_URL="http://$CHECK_HOST:$PORT"
if up "$ADAPTER_URL/health"; then
  echo "something already answers on $ADAPTER_URL; stop it first (--stop)" >&2; exit 1
fi
LOG="$RUN_DIR/adapter.log"
cd "$ROOT"
PID="$(start_bg "$LOG" "$PYTHON" "$ROOT/scripts/aml_adapter.py" --config "$ROOT/deploy/aml/config.yml" --host "$BIND_HOST" --port "$PORT")"
echo "adapter $PID" >> "$PID_FILE"
wait_up adapter "$ADAPTER_URL/health" 180 "$PID" "$LOG.err"
echo "ready: POST http://$BIND_HOST:$PORT/add and http://$BIND_HOST:$PORT/search (in-memory=$AML_IN_MEMORY); logs in $RUN_DIR"
