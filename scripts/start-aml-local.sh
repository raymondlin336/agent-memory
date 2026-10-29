#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Local AML stack on Linux: MiniLM rerank server + AML Add/Search adapter (in-memory by default).
# Linux counterpart of scripts/start-aml-local.ps1.
#
#   scripts/start-aml-local.sh            # start, wait until healthy
#   scripts/start-aml-local.sh --stop     # stop what it started
#
# Settings come from deploy/aml/.env (falls back to evaluation/environment/.env). API_KEY /
# EMBED_API_KEY may instead be read from files named by API_KEY_FILE / EMBED_API_KEY_FILE, so keys
# stay out of the repo and off the command line. Logs, pids and the ledger go to
# evaluation/outputs/aml_local/ (git-ignored).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${ROOT}/evaluation/outputs/aml_local"
PORT=8200
BIND_HOST=127.0.0.1
ENV_FILE=""
STOP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stop) STOP=1 ;;
    --port) PORT="$2"; shift ;;
    --host) BIND_HOST="$2"; shift ;;
    --env-file) ENV_FILE="$2"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
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
fi
[[ -f "${ENV_FILE}" ]] || { echo "env file not found: ${ENV_FILE} (cp deploy/aml/.env.example deploy/aml/.env)" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

read_key_file() {  # VAR FILE_VAR
  local var="$1" file_var="$2"
  if [[ -z "${!var:-}" && -n "${!file_var:-}" ]]; then
    [[ -r "${!file_var}" ]] || { echo "${file_var}: cannot read ${!file_var}" >&2; exit 1; }
    export "${var}=$(tr -d '[:space:]' < "${!file_var}")"
  fi
}
read_key_file API_KEY API_KEY_FILE
read_key_file EMBED_API_KEY EMBED_API_KEY_FILE
[[ -n "${API_KEY:-}" ]] || { echo "API_KEY (or API_KEY_FILE) is not set" >&2; exit 1; }
[[ -n "${EMBED_API_KEY:-}" ]] || { echo "EMBED_API_KEY (or EMBED_API_KEY_FILE) is not set" >&2; exit 1; }

# The key is required on every address; the adapter refuses empty, short or placeholder keys.
[[ -n "${AML_ADAPTER_KEY:-}" ]] || { echo "AML_ADAPTER_KEY is not set in ${ENV_FILE} (32+ random characters; see deploy/aml/README-linux.md)" >&2; exit 1; }
export AML_IN_MEMORY="${AML_IN_MEMORY:-1}"
export AML_STATE_DB="${AML_STATE_DB:-${RUN_DIR}/aml_adapter_state.db}"
export RERANK_BASE_URL="${RERANK_BASE_URL:-http://127.0.0.1:8906/v1}"
export RERANK_API_KEY="${RERANK_API_KEY:-local}"
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
