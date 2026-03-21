#!/usr/bin/env bash

set -euo pipefail

project="${PROJECT:-bug-request}"
region="${REGION:-europe-west1}"
zone="${ZONE:-europe-west1-b}"
engine="${ENGINE:-engine}"
database="${DATABASE:-bug-request-database}"

usage() {
  cat <<'EOF'
Usage: ./control.sh <start|stop>

Optional environment overrides:
  PROJECT
  REGION
  ZONE
  ENGINE
  DATABASE
EOF
}

require_gcloud() {
  if ! command -v gcloud >/dev/null 2>&1; then
    echo "gcloud is required but not installed or not on PATH." >&2
    exit 1
  fi
}

log() {
  printf '[control] %s\n' "$*"
}

describe_database_state() {
  gcloud sql instances describe "${database}" \
    --project="${project}" \
    --format='value(state)'
}

describe_database_policy() {
  gcloud sql instances describe "${database}" \
    --project="${project}" \
    --format='value(settings.activationPolicy)'
}

describe_engine_state() {
  gcloud compute instances describe "${engine}" \
    --project="${project}" \
    --zone="${zone}" \
    --format='value(status)'
}

wait_for_operation() {
  local operation_id="$1"

  log "Waiting for Cloud SQL operation ${operation_id}"
  gcloud sql operations wait "${operation_id}" \
    --project="${project}" \
    --timeout=unlimited
}

patch_database_policy() {
  local policy="$1"
  local output
  local operation_id

  set +e
  output="$(
    gcloud sql instances patch "${database}" \
      --activation-policy="${policy}" \
      --project="${project}" \
      2>&1
  )"
  local status=$?
  set -e

  printf '%s\n' "${output}"

  if [[ ${status} -eq 0 ]]; then
    return 0
  fi

  if [[ "${output}" == *"taking longer than expected"* ]]; then
    operation_id="$(
      printf '%s\n' "${output}" \
        | sed -n 's#.*operations/\([^[:space:]]*\).*#\1#p' \
        | tail -n 1
    )"

    if [[ -z "${operation_id}" ]]; then
      echo "Cloud SQL patch timed out and the operation ID could not be parsed." >&2
      return 1
    fi

    wait_for_operation "${operation_id}"
    return 0
  fi

  return "${status}"
}

wait_for_database() {
  local wait_seconds=600
  local sleep_seconds=5
  local deadline
  local state

  deadline=$((SECONDS + wait_seconds))

  while (( SECONDS < deadline )); do
    state="$(describe_database_state)"
    if [[ "${state}" == "RUNNABLE" ]]; then
      return 0
    fi
    log "Waiting for database ${database} in ${region}. Current state: ${state:-unknown}"
    sleep "${sleep_seconds}"
  done

  echo "Timed out waiting for database ${database} to become RUNNABLE." >&2
  return 1
}

print_status() {
  local database_state
  local database_policy
  local engine_state

  database_state="$(describe_database_state)"
  database_policy="$(describe_database_policy)"
  engine_state="$(describe_engine_state)"

  log "Database state: ${database_state:-unknown}"
  log "Database activation policy: ${database_policy:-unknown}"
  log "Engine state: ${engine_state:-unknown}"
}

start_stack() {
  log "Starting database ${database} in ${region}"
  patch_database_policy "ALWAYS"

  wait_for_database

  log "Starting engine ${engine} in ${zone}"
  gcloud compute instances start "${engine}" \
    --project="${project}" \
    --zone="${zone}"

  print_status
}

stop_stack() {
  log "Stopping engine ${engine} in ${zone}"
  gcloud compute instances stop "${engine}" \
    --project="${project}" \
    --zone="${zone}"

  log "Stopping database ${database} in ${region}"
  patch_database_policy "NEVER"

  print_status
}

main() {
  if [[ $# -ne 1 ]]; then
    usage
    exit 1
  fi

  require_gcloud

  case "$1" in
    start)
      start_stack
      ;;
    stop)
      stop_stack
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
