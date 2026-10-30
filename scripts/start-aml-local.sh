#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Local AML stack on Linux: MiniLM rerank server + AML Add/Search adapter (in-memory by default).
# Linux counterpart of scripts/start-aml-local.ps1.
#
#   scripts/start-aml-local.sh            # start, wait until healthy
#   scripts/start-aml-local.sh --stop     # stop what it started
#
# More examples (own ports / keys, test commands): scripts/start-aml-local.example.txt
#
# Precedence: command-line options > variables already set in the environment > deploy/aml/.env
# (falls back to evaluation/environment/.env; optional) > defaults from deploy/aml/.env.example.
# API_KEY / EMBED_API_KEY may instead be read from files named by API_KEY_FILE / EMBED_API_KEY_FILE,
# so keys stay out of the repo and off the command line. Logs, pids and the ledger go to
# evaluation/outputs/aml_local/ (git-ignored).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${ROOT}/evaluation/outputs/aml_local"
PORT=""
BIND_HOST=127.0.0.1
ENV_FILE=""
STOP=0
OPT_RERANK_PORT="" OPT_API_BASE="" OPT_MODEL="" OPT_API_KEY_FILE="" OPT_EMBED_KEY_FILE="" OPT_API_KEY="" OPT_EMBED_KEY=""

usage() {
  sed -n '3,/^set -e/{/^set -e/!p}' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'
Options:
  --stop                  stop the reranker and adapter started by this script
  --port N                adapter port (default: AML_PORT from env / .env, else 8200)
  --host H                adapter bind address (default 127.0.0.1)
  --rerank-port N         reranker port (default 8906; sets RERANK_BASE_URL to 127.0.0.1:N)
  --api-base URL          OpenAI-compatible LLM endpoint (sets API_BASE)
  --model NAME            LLM model name (sets MODEL_NAME; default gpt-4o-mini, openai/gpt-4o-mini on OpenRouter)
  --api-key KEY           LLM key itself (sets API_KEY; visible in shell history, prefer a file)
  --api-key-file PATH     file holding the LLM key (sets API_KEY from it)
  --embed-key KEY         embedding key itself (sets EMBED_API_KEY)
  --embed-key-file PATH   file holding the embedding key (sets EMBED_API_KEY from it)
  --env-file PATH         env file to load instead of deploy/aml/.env
EOF
}

need_value() { [[ $# -ge 2 && -n "$2" ]] || { echo "$1 needs a value" >&2; exit 2; }; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stop) STOP=1 ;;
    --port) need_value "$@"; PORT="$2"; shift ;;
    --host) need_value "$@"; BIND_HOST="$2"; shift ;;
    --rerank-port) need_value "$@"; OPT_RERANK_PORT="$2"; shift ;;
    --api-base) need_value "$@"; OPT_API_BASE="$2"; shift ;;
    --model) need_value "$@"; OPT_MODEL="$2"; shift ;;
    --api-key) need_value "$@"; OPT_API_KEY="$2"; shift ;;
    --api-key-file) need_value "$@"; OPT_API_KEY_FILE="$2"; shift ;;
    --embed-key) need_value "$@"; OPT_EMBED_KEY="$2"; shift ;;
    --embed-key-file) need_value "$@"; OPT_EMBED_KEY_FILE="$2"; shift ;;
    --env-file) need_value "$@"; ENV_FILE="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
for p in "${PORT}" "${OPT_RERANK_PORT}"; do
  [[ -z "${p}" || "${p}" =~ ^[0-9]+$ ]] || { echo "port must be a number: ${p}" >&2; exit 2; }
done
mkdir -p "${RUN_DIR}"

stop_started() {
  local found=0
  for name in adapter reranker; do
    local pidfile="${RUN_DIR}/${name}.pid"
    [[ -f "${pidfile}" ]] || continue
    found=1
    local pid
    pid="$(cat "${pidfile}")"
    if kill -0 "${pid}" 2>/dev/null; then
      kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "${pid}"   # whole process group
      for _ in $(seq 1 20); do kill -0 "${pid}" 2>/dev/null || break; sleep 0.5; done
      if kill -0 "${pid}" 2>/dev/null; then kill -KILL -- "-${pid}" 2>/dev/null || true; fi
      echo "stopped ${name} (pid ${pid})"
    fi
    rm -f "${pidfile}"
  done
  [[ ${found} -eq 1 ]] || echo "nothing recorded in ${RUN_DIR}"
}

is_up() { curl -sf --max-time 3 "$1" >/dev/null 2>&1; }

wait_up() {  # name url pid log
  local name="$1" url="$2" pid="$3" log="$4"
  for _ in $(seq 1 90); do
    if is_up "${url}"; then echo "${name} up: ${url}"; return 0; fi
    kill -0 "${pid}" 2>/dev/null || break
    sleep 2
  done
  echo "${name} did not come up; last log lines from ${log}:" >&2
  tail -n 20 "${log}" >&2 || true
  exit 1
}

start_bg() {  # name log cmd...   (own session so --stop can kill the whole group)
  local name="$1" log="$2"; shift 2
  setsid nohup "$@" >"${log}" 2>&1 < /dev/null &
  echo $! >"${RUN_DIR}/${name}.pid"
}

if [[ ${STOP} -eq 1 ]]; then stop_started; exit 0; fi

if [[ -z "${ENV_FILE}" ]]; then
  ENV_FILE="${ROOT}/deploy/aml/.env"
  [[ -f "${ENV_FILE}" ]] || ENV_FILE="${ROOT}/evaluation/environment/.env"
elif [[ ! -f "${ENV_FILE}" ]]; then
  echo "env file not found: ${ENV_FILE}" >&2; exit 1
fi
# Load KEY=VALUE lines without overriding variables already set (non-empty) in the environment,
# like start-aml-local.ps1. A missing default env file is fine when everything comes from the
# command line or the environment.
if [[ -f "${ENV_FILE}" ]]; then
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    [[ "${line}" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}" value="${BASH_REMATCH[2]}"
    value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
    [[ "${value}" =~ ^\"(.*)\"$ || "${value}" =~ ^\'(.*)\'$ ]] && value="${BASH_REMATCH[1]}"
    [[ -n "${!key:-}" ]] || export "${key}=${value}"
  done < "${ENV_FILE}"
  echo "settings: ${ENV_FILE} (values already set in the environment win)"
else
  echo "no env file; using command-line options, environment and defaults"
fi

# Command-line options win over both.
PORT="${PORT:-${AML_PORT:-8200}}"
[[ -n "${OPT_API_BASE}" ]] && export API_BASE="${OPT_API_BASE}"
[[ -n "${OPT_MODEL}" ]] && export MODEL_NAME="${OPT_MODEL}"
[[ -n "${OPT_RERANK_PORT}" ]] && export RERANK_BASE_URL="http://127.0.0.1:${OPT_RERANK_PORT}/v1"
[[ -n "${OPT_API_KEY_FILE}" ]] && export API_KEY="" API_KEY_FILE="${OPT_API_KEY_FILE}"
[[ -n "${OPT_EMBED_KEY_FILE}" ]] && export EMBED_API_KEY="" EMBED_API_KEY_FILE="${OPT_EMBED_KEY_FILE}"
[[ -n "${OPT_API_KEY}" ]] && export API_KEY="${OPT_API_KEY}"
[[ -n "${OPT_EMBED_KEY}" ]] && export EMBED_API_KEY="${OPT_EMBED_KEY}"

read_key_file() {  # VAR FILE_VAR
  local var="$1" file_var="$2"
  if [[ -z "${!var:-}" && -n "${!file_var:-}" ]]; then
    [[ -r "${!file_var}" ]] || { echo "${file_var}: cannot read ${!file_var}" >&2; exit 1; }
    export "${var}=$(tr -d '[:space:]' < "${!file_var}")"
  fi
}
read_key_file API_KEY API_KEY_FILE
read_key_file EMBED_API_KEY EMBED_API_KEY_FILE
[[ -n "${API_KEY:-}" ]] || { echo "LLM key missing: use --api-key / --api-key-file, API_KEY / API_KEY_FILE, or deploy/aml/.env" >&2; exit 1; }
[[ -n "${EMBED_API_KEY:-}" ]] || { echo "embedding key missing: use --embed-key / --embed-key-file, EMBED_API_KEY / EMBED_API_KEY_FILE, or deploy/aml/.env" >&2; exit 1; }

# Defaults (same values as deploy/aml/.env.example) for anything still unset.
export API_BASE="${API_BASE:-https://api.openai.com/v1}"
if [[ -z "${MODEL_NAME:-}" ]]; then
  # OpenRouter names models by provider: openai/gpt-4o-mini
  if [[ "${API_BASE}" == *openrouter.ai* ]]; then MODEL_NAME=openai/gpt-4o-mini; else MODEL_NAME=gpt-4o-mini; fi
  export MODEL_NAME
fi
export EMBED_BASE_URL="${EMBED_BASE_URL:-https://dashscope-intl.aliyuncs.com/compatible-mode/v1}"
export EMBED_MODEL_NAME="${EMBED_MODEL_NAME:-text-embedding-v4}"
# The adapter key is required on every address; the adapter refuses empty, short or placeholder keys.
[[ -n "${AML_ADAPTER_KEY:-}" ]] || { echo "AML_ADAPTER_KEY is not set: put it in deploy/aml/.env or the environment (32+ random characters; see deploy/aml/README-linux.md)" >&2; exit 1; }
export AML_IN_MEMORY="${AML_IN_MEMORY:-1}"
export AML_STATE_DB="${AML_STATE_DB:-${RUN_DIR}/aml_adapter_state.db}"
export RERANK_BASE_URL="${RERANK_BASE_URL:-http://127.0.0.1:8906/v1}"
export RERANK_API_KEY="${RERANK_API_KEY:-local}"
echo "adapter port ${PORT}, reranker ${RERANK_BASE_URL}, LLM ${MODEL_NAME} @ ${API_BASE}"
export PYTHONPATH="${ROOT}" PYTHONIOENCODING=utf-8
PYTHON="${PYTHON:-${ROOT}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || { echo "python not found: ${PYTHON} (python3 -m venv .venv && .venv/bin/pip install -e \".[dev,rerank]\")" >&2; exit 1; }

rerank_url="${RERANK_BASE_URL%/}"
if is_up "${rerank_url}"; then
  echo "reranker already up: ${rerank_url}"
else
  rerank_port="$(sed -E 's#^[a-z]+://[^:/]+:?([0-9]*).*#\1#' <<<"${rerank_url}")"
  log="${RUN_DIR}/reranker.log"
  start_bg reranker "${log}" "${RERANK_SERVER_PYTHON:-${PYTHON}}" \
    "${RERANK_SERVER_SCRIPT:-${ROOT}/scripts/rerank_server.py}" --port "${rerank_port:-8906}"
  wait_up reranker "${rerank_url}" "$(cat "${RUN_DIR}/reranker.pid")" "${log}"
fi

adapter_url="http://${BIND_HOST}:${PORT}"
if is_up "${adapter_url}/health"; then echo "something already answers on ${adapter_url}; stop it first (--stop)" >&2; exit 1; fi
log="${RUN_DIR}/adapter.log"
start_bg adapter "${log}" "${PYTHON}" "${ROOT}/scripts/aml_adapter.py" \
  --config "${ROOT}/deploy/aml/config.yml" --host "${BIND_HOST}" --port "${PORT}"
wait_up adapter "${adapter_url}/health" "$(cat "${RUN_DIR}/adapter.pid")" "${log}"
echo "ready: POST ${adapter_url}/add and ${adapter_url}/search (in-memory=${AML_IN_MEMORY}); logs in ${RUN_DIR}"
