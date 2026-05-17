#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ENV_FILE_INPUT="${ENV_FILE:-docs/public/examples/openloadhub-host.env.example}"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
ADMIN_PORT="${ADMIN_PORT:-18000}"
FRONTEND_PORT="${FRONTEND_PORT:-13000}"

if [[ "${ENV_FILE_INPUT}" = /* ]]; then
  ENV_FILE_PATH="${ENV_FILE_INPUT}"
else
  ENV_FILE_PATH="${ROOT_DIR}/${ENV_FILE_INPUT}"
fi

usage() {
  cat <<'EOF'
Usage:
  scripts/start-host-apps.sh <ptp-admin|ptp-worker|frontend>

This starts host application processes for the OpenLoadHub public demo while
middleware, agents, and the demo target keep running in Docker.

Defaults:
  ENV_FILE=docs/public/examples/openloadhub-host.env.example
  ADMIN_PORT=18000
  FRONTEND_PORT=13000

Examples:
  ENV_FILE=docs/public/examples/openloadhub-host.env.example ./scripts/start-host-apps.sh ptp-admin
  ENV_FILE=docs/public/examples/openloadhub-host.env.example ./scripts/start-host-apps.sh ptp-worker
  ENV_FILE=docs/public/examples/openloadhub-host.env.example ./scripts/start-host-apps.sh frontend

If the Docker frontend/admin/worker containers are still running, stop them
first so the host processes clearly serve the public demo ports:
  docker compose -f docker-compose.demo.yml stop frontend ptp-admin ptp-worker
EOF
}

port_for_service() {
  case "$1" in
    ptp-admin) printf '%s\n' "${ADMIN_PORT}" ;;
    frontend) printf '%s\n' "${FRONTEND_PORT}" ;;
    *) return 1 ;;
  esac
}

ensure_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "Missing required file: ${path}" >&2
    exit 1
  fi
}

ensure_executable() {
  local path="$1"
  local label="$2"
  if [[ ! -x "${path}" ]]; then
    echo "Missing executable ${label}: ${path}" >&2
    echo "Create a local virtualenv and install backend requirements before starting host mode." >&2
    exit 1
  fi
}

ensure_target_port_free() {
  local service="$1"
  local port listeners

  if [[ "${ALLOW_HOST_SERVICE_PORT_CONFLICT:-0}" == "1" ]]; then
    return 0
  fi
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  if ! port="$(port_for_service "${service}")"; then
    return 0
  fi

  listeners="$(lsof -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -z "${listeners}" ]]; then
    return 0
  fi

  echo "Refusing to start host ${service}: port ${port} is already in use." >&2
  echo "${listeners}" >&2
  echo "Stop the Docker app container or old host process first, then retry." >&2
  exit 1
}

check_connectivity() {
  if [[ "${CHECK_CONNECTIVITY:-0}" != "1" ]]; then
    return 0
  fi
  if ! command -v nc >/dev/null 2>&1; then
    echo "CHECK_CONNECTIVITY=1 requires nc, but nc was not found." >&2
    exit 1
  fi

  local endpoints=(
    "127.0.0.1:${MYSQL_PORT:-13306}:mysql"
    "127.0.0.1:${REDIS_PORT:-16379}:redis"
    "127.0.0.1:${GRAFANA_PORT:-13001}:grafana"
    "127.0.0.1:${PROMETHEUS_PORT:-19090}:prometheus"
    "127.0.0.1:${PUSHGATEWAY_PORT:-19091}:pushgateway"
  )
  local agent_endpoints
  if ! agent_endpoints="$(agent_connectivity_endpoints)"; then
    exit 1
  fi
  if [[ -z "${agent_endpoints}" ]]; then
    echo "AGENT_HOSTS did not provide any agent endpoints for host connectivity check." >&2
    exit 1
  fi
  while IFS= read -r item; do
    endpoints+=("${item}")
  done <<<"${agent_endpoints}"
  local item host port label
  for item in "${endpoints[@]}"; do
    IFS=: read -r host port label <<<"${item}"
    if ! nc -z "${host}" "${port}" >/dev/null 2>&1; then
      echo "Connectivity check failed for ${label} at ${host}:${port}" >&2
      exit 1
    fi
  done
}

agent_connectivity_endpoints() {
  local raw_hosts="${AGENT_HOSTS:-127.0.0.1:19096,127.0.0.1:19097,127.0.0.1:19098,127.0.0.1:19099}"
  local old_ifs="${IFS}"
  local entries=()
  local index=1
  local entry host port
  IFS=,
  read -ra entries <<<"${raw_hosts}"
  IFS="${old_ifs}"
  for entry in "${entries[@]}"; do
    entry="${entry//[[:space:]]/}"
    if [[ -z "${entry}" ]]; then
      continue
    fi
    host="${entry%:*}"
    port="${entry##*:}"
    if [[ -z "${host}" || -z "${port}" || "${host}" == "${entry}" ]]; then
      echo "Invalid AGENT_HOSTS entry for host connectivity check: ${entry}" >&2
      exit 1
    fi
    printf '%s:%s:agent-%s\n' "${host}" "${port}" "${index}"
    index=$((index + 1))
  done
}

resolve_root_relative_path() {
  local value="$1"
  if [[ "${value}" = /* ]]; then
    printf '%s\n' "${value}"
  else
    printf '%s\n' "${ROOT_DIR}/${value}"
  fi
}

sync_script_volume_to_host_mirror() {
  if [[ -z "${PTP_LOCAL_SCRIPT_MIRROR_DIR:-}" ]]; then
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi

  local compose_project="${OPENLOADHUB_COMPOSE_PROJECT_NAME:-openloadhub-demo}"
  local volume_name="${compose_project}_tmp-scripts"
  local image_name="${compose_project}-ptp-agent"
  if ! docker volume inspect "${volume_name}" >/dev/null 2>&1; then
    return 0
  fi
  if ! docker image inspect "${image_name}" >/dev/null 2>&1; then
    return 0
  fi

  docker run --rm \
    -v "${volume_name}:/src:ro" \
    -v "${PTP_LOCAL_SCRIPT_MIRROR_DIR}:/dst" \
    "${image_name}" \
    sh -c 'mkdir -p /dst && cp -an /src/. /dst/ 2>/dev/null || true' >/dev/null 2>&1 || true
}

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

SERVICE="$1"
case "${SERVICE}" in
  ptp-admin|ptp-worker|frontend) ;;
  *)
    usage
    exit 1
    ;;
esac

ensure_file "${ENV_FILE_PATH}"
ensure_target_port_free "${SERVICE}"

set -a
source "${ENV_FILE_PATH}"
set +a

PTP_LOCAL_SCRIPT_MIRROR_DIR="$(
  resolve_root_relative_path "${PTP_LOCAL_SCRIPT_MIRROR_DIR:-.tmp/openloadhub-host-scripts}"
)"
export PTP_LOCAL_SCRIPT_MIRROR_DIR
mkdir -p "${PTP_LOCAL_SCRIPT_MIRROR_DIR}"
sync_script_volume_to_host_mirror

check_connectivity

case "${SERVICE}" in
  ptp-admin)
    ensure_executable "${PYTHON_BIN}" "python"
    "${PYTHON_BIN}" -c "import uvicorn" >/dev/null
    cd "${ROOT_DIR}/backend/ptp-admin"
    export PYTHONPATH="${ROOT_DIR}/backend:${ROOT_DIR}/backend/ptp-admin:${PYTHONPATH:-}"
    exec "${PYTHON_BIN}" -m uvicorn main:app --host 127.0.0.1 --port "${ADMIN_PORT}"
    ;;
  ptp-worker)
    ensure_executable "${PYTHON_BIN}" "python"
    cd "${ROOT_DIR}/backend/ptp-worker"
    export PYTHONPATH="${ROOT_DIR}/backend:${ROOT_DIR}/backend/ptp-admin:${PYTHONPATH:-}"
    exec "${PYTHON_BIN}" celery_worker.py
    ;;
  frontend)
    if ! command -v npm >/dev/null 2>&1; then
      echo "Missing npm. Install Node.js dependencies before starting the frontend." >&2
      exit 1
    fi
    cd "${ROOT_DIR}/frontend"
    export API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:${ADMIN_PORT}}"
    exec npm run dev -- --host 127.0.0.1 --port "${FRONTEND_PORT}"
    ;;
esac
