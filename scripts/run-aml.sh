#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# AML 排行榜 Add/Search 适配器启动脚本；环境变量见 deploy/aml/.env.example。
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${REPO_ROOT}/deploy/aml/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/deploy/aml/.env"
  set +a
fi
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
export AML_CONFIG="${AML_CONFIG:-${REPO_ROOT}/deploy/aml/config.yml}"
exec python3 "${REPO_ROOT}/scripts/aml_adapter.py" "$@"
