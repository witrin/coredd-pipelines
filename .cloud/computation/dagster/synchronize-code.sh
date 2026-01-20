#!/usr/bin/env bash
set -euo pipefail

locations="/etc/dagster/locations"
workspace="/etc/dagster/workspace.yaml"
unit="dagster-location"
lock="/run/dagster-synchronize.lock"
secrets="/etc/dagster/secrets.env"

usage() {
  cat <<'EOF'
Usage: dagster-locations-synchronize --project <project> --region <region> --bucket <bucket> --database <database> --ports <start>-<end> --account <account> --prefix <prefix> [code registry] [job registry]
EOF
}

die() {
  echo "error: $1" >&2
  usage >&2
  exit 1
}

require_option_value() {
  if [[ $# -lt 2 || -z "$2" ]]; then
    die "Missing value for $1"
  fi
}

require_arg() {
  if [[ -z "$1" ]]; then
    die "Missing $2"
  fi
}

parse_ports() {
  local range=$1
  if [[ ! "$range" =~ ^[0-9]+-[0-9]+$ ]]; then
    die "Ports must be START-END"
  fi
  port_start="${range%-*}"
  port_end="${range#*-}"
  if (( port_start < 1 || port_start > 65535 || port_end < 1 || port_end > 65535 || port_start > port_end )); then
    die "Invalid port range"
  fi
}

parse_database_uri() {
  local uri=$1
  local remainder user host path

  if [[ "$uri" != *"://"* ]]; then
    die "Database URI must include a scheme"
  fi
  remainder="${uri#*://}"
  if [[ "$remainder" != *"/"* ]]; then
    die "Database URI must include a database name"
  fi
  if [[ "$remainder" == *"@"* ]]; then
    user="${remainder%%@*}"
    host="${remainder#*@}"
  else
    die "Database URI must include a user"
  fi
  database_user="${user%%:*}"
  if [[ -z "$database_user" ]]; then
    die "Database URI user is empty"
  fi
  database_host="${host%%/*}"
  if [[ -z "$database_host" ]]; then
    die "Database URI host is empty"
  fi
  path="${host#*/}"
  database_name="${path%%\\?*}"
  if [[ -z "$database_name" ]]; then
    die "Database URI name is empty"
  fi

  if [[ "$path" == *"?"* ]]; then
    local query key value pair
    local -a pairs
    query="${path#*\\?}"
    IFS='&' read -r -a pairs <<< "$query"
    for pair in "${pairs[@]}"; do
      key="${pair%%=*}"
      value="${pair#*=}"
      if [[ "$key" == "password" ]]; then
        database_password="$value"
      fi
    done
  fi
  if [[ -z "$database_password" ]]; then
    die "Database URI password key is empty"
  fi
}

ports=""
database=""
project=""
region=""
account=""
bucket=""
prefix=""
code_image_repository=""
job_image_repository=""
database_user=""
database_name=""
database_host=""
database_password=""
port_start=""
port_end=""
secrets_changed=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      require_option_value "$1" "${2-}"
      project="$2"
      shift 2
      ;;
    --region)
      require_option_value "$1" "${2-}"
      region="$2"
      shift 2
      ;;
    --bucket)
      require_option_value "$1" "${2-}"
      bucket="$2"
      shift 2
      ;;
    --database)
      require_option_value "$1" "${2-}"
      database="$2"
      shift 2
      ;;
    --ports)
      require_option_value "$1" "${2-}"
      ports="$2"
      shift 2
      ;;
    --account)
      require_option_value "$1" "${2-}"
      account="$2"
      shift 2
      ;;
    --prefix)
      require_option_value "$1" "${2-}"
      prefix="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      die "Unknown option: $1"
      ;;
    *)
      break
      ;;
  esac
done

if [[ $# -ne 2 ]]; then
  die "Expected code and job registry"
fi

code_image_repository="$1"
job_image_repository="$2"
shift 2

require_arg "$project" "--project"
require_arg "$region" "--region"
require_arg "$bucket" "--bucket"
require_arg "$database" "--database"
require_arg "$ports" "--ports"
require_arg "$account" "--account"
require_arg "$prefix" "--prefix"
require_arg "$code_image_repository" "code registry"
require_arg "$job_image_repository" "job registry"

parse_ports "$ports"
parse_database_uri "$database"

mkdir -p "$(dirname "$lock")"
exec 9>"$lock"
if ! flock -n 9; then
  exit 0
fi

ensure_dirs() {
  mkdir -p "$locations"
}

sync_secrets() {
  local output tmp key name value
  local -a keys=()
  output=$(gcloud secrets list --project="$project" --format="value(name)") || die "Failed to list secrets"
  if [[ -n "$output" ]]; then
    mapfile -t keys < <(printf '%s\n' "$output" | sort)
  fi
  tmp=$(mktemp)
  chmod 600 "$tmp"
  for key in "${keys[@]}"; do
    [[ -n "$key" ]] || continue
    if [[ "$key" != "$prefix"* ]]; then
      continue
    fi
    name=$(gcloud secrets describe "$key" \
      --project="$project" \
      --format="value(labels.environment)" 2>/dev/null || true)
    if [[ -z "$name" ]]; then
      echo "Secret $key missing label environment" >&2
      continue
    fi
    if [[ ! "$name" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
      echo "Secret $key label environment is not a valid env name: $name" >&2
      continue
    fi
    value=$(gcloud secrets versions access latest --secret="$key" --project="$project" 2>/dev/null || true)
    if [[ -z "$value" ]]; then
      echo "Secret $key value is empty" >&2
    fi
    printf '%s=%s\n' "$name" "$value" >>"$tmp"
  done
  mkdir -p "$(dirname "$secrets")"
  if [[ ! -f "$secrets" ]] || ! cmp -s "$tmp" "$secrets"; then
    mv "$tmp" "$secrets"
    chmod 600 "$secrets"
    secrets_changed=1
  fi
  rm -f "$tmp"
}

restart_location_services() {
  local -a units=()
  mapfile -t units < <(
    systemctl list-units --full --state=active "$unit@*.service" --no-legend 2>/dev/null | awk '{print $1}'
  )
  for location_unit in "${units[@]}"; do
    [[ -n "$location_unit" ]] || continue
    systemctl restart "$location_unit" || true
  done
}

assign_port() {
  local used_ports
  used_ports=$(grep -h '^GRPC_PORT=' "$locations"/*.env 2>/dev/null | cut -d= -f2 || true)
  local port="$port_start"
  while true; do
    if [[ -n "$port_end" && "$port" -gt "$port_end" ]]; then
      echo ""
      return 1
    fi
    if ! grep -qx "$port" <<<"$used_ports" 2>/dev/null; then
      echo "$port"
      return 0
    fi
    port=$((port + 1))
  done
}

get_env_digest() {
  local configuration=$1
  local digest_line
  if [[ -f "$configuration" ]]; then
    digest_line=$(grep -m1 '^# DIGEST=' "$configuration" || true)
    if [[ -n "$digest_line" ]]; then
      echo "${digest_line#*=}"
    fi
  fi
}

set_env_digest() {
  local configuration=$1
  local digest=$2
  local tmp
  if [[ -f "$configuration" ]]; then
    if grep -q '^# DIGEST=' "$configuration"; then
      sed -i "s|^# DIGEST=.*|# DIGEST=$digest|" "$configuration"
      return
    fi
  fi
  tmp=$(mktemp)
  {
    printf '# DIGEST=%s\n' "$digest"
    if [[ -f "$configuration" ]]; then
      cat "$configuration"
    fi
  } >"$tmp"
  mv "$tmp" "$configuration"
}

slugify_branch() {
  local branch=$1
  local slug
  slug=$(echo "$branch" | tr '[:upper:]' '[:lower:]')
  slug=$(echo "$slug" | sed 's/[^a-z0-9-]/-/g')
  slug=$(echo "$slug" | sed 's/-\{2,\}/-/g')
  slug="${slug#-}"
  slug="${slug%-}"
  if [[ -z "$slug" ]]; then
    return 1
  fi
  printf '%s\n' "$slug"
}

cloud_run_job_name() {
  local branch=$1
  local slug base max_length
  slug=$(slugify_branch "$branch") || return 1
  base="pipelines"
  max_length=$((63 - ${#base} - 1))
  if (( max_length < 1 )); then
    echo "Skip job $branch (job name too long)" >&2
    return 1
  fi
  slug=${slug:0:max_length}
  slug="${slug%-}"
  if [[ -z "$slug" ]]; then
    echo "Skip job $branch (slug empty)" >&2
    return 1
  fi

  printf '%s-%s\n' "$base" "$slug"
}

ensure_cloud_run_job() {
  local branch=$1
  local job image current_image
  job=$(cloud_run_job_name "$branch") || {
    echo "Skip job $branch (invalid name)"
    return 0
  }
  image="${job_image_repository}:$branch"
  active_jobs["$job"]=1
  current_image=$(gcloud run jobs describe "$job" \
    --project="$project" \
    --region="$region" \
    --format='value(template.template.containers[0].image)' 2>/dev/null || true)
  if [[ "$current_image" == "$image" ]]; then
    echo "Job ok $branch ($job)"
    return 0
  fi
  echo "Deploy job $branch ($job)"
  gcloud run jobs deploy "$job" \
    --project="$project" \
    --region="$region" \
    --image="$image" \
    --service-account="$account" \
    --tasks=1 \
    --parallelism=1 \
    --max-retries=0 \
    --task-timeout=3600s \
    --memory=4Gi \
    --set-cloudsql-instances="${database_host}" \
    --set-env-vars="DAGSTER_HOME=/etc/dagster" \
    --set-env-vars="DATABASE_HOST=/cloudsql/${database_host}" \
    --set-env-vars="DATABASE_USER=${database_user}" \
    --set-env-vars="DATABASE_NAME=${database_name}" \
    --set-secrets="DATABASE_PASSWORD=${database_password}" \
    --set-secrets="GITHUB_TOKEN=github-dagster-api-secret:latest" \
    --set-env-vars="CLOUD_BUCKET=${bucket}" \
    --set-env-vars="DAGSTER_LOCATION=$branch" \
    --quiet
}

cleanup_cloud_run_jobs() {
  local -a existing_jobs=()
  local job prefix regex
  prefix="pipelines"
  regex=$(printf '%s\n' "$prefix" | sed -e 's/[][(){}.+?^$\\|-]/\\&/g')
  mapfile -t existing_jobs < <(
    gcloud run jobs list \
      --project="$project" \
      --region="$region" \
      --format='value(name)' \
      --filter="name~^${regex}" 2>/dev/null || true
  )
  for job in "${existing_jobs[@]}"; do
    [[ -n "$job" ]] || continue
    if [[ -n "${active_jobs[$job]+x}" ]]; then
      continue
    fi
    echo "Delete job $job"
    gcloud run jobs delete "$job" \
      --project="$project" \
      --region="$region" \
      --quiet || true
  done
}

update_workspace() {
  local tmp branch port configuration
  local -a workspace_branches=()
  tmp=$(mktemp)
  {
    echo "load_from:"
    mapfile -t workspace_branches < <(
      printf '%s\n' ${!active_digests[@]} | sort
    )
    for branch in "${workspace_branches[@]}"; do
      configuration="$locations/$branch.env"
      [ -f "$configuration" ] || continue
      if ! grep -q '^GRPC_PORT=' "$configuration"; then
        continue
      fi
      port=$(grep '^GRPC_PORT=' "$configuration" | tail -n1 | cut -d= -f2)
      printf '  - grpc_server:\n'
      printf '      host: 127.0.0.1\n'
      printf '      port: %s\n' "$port"
      printf '      location_name: %s\n' "$branch"
    done
  } >"$tmp"

  if [[ ! -f "$workspace" ]] || ! cmp -s "$tmp" "$workspace"; then
    cat "$tmp" > "$workspace"
    chmod 644 "$workspace"
    systemctl restart dagster-interface.service
  fi
  
  rm -f "$tmp"
}

images_json=$(
  gcloud artifacts docker images list "${code_image_repository}" \
    --include-tags \
    --format=json \
    --project="$project"
)

declare -A active_digests
declare -A active_jobs
while IFS=':' read -r branch digest; do
  active_digests["$branch"]="$digest"
done < <(
  echo "$images_json" | jq -r '
    .[]
    | select(.tags != null)
    | (.version // .name // "") as $v
    | ($v | try (match("sha256:[a-f0-9]+") | .string) catch "") as $d
    | select($d != "")
    | .tags[]
    | select(test("^[A-Za-z0-9._-]+$"))
    | . + ":" + $d
  '
)

ensure_dirs
sync_secrets

for branch in "${!active_digests[@]}"; do
  digest="${active_digests[$branch]}"
  configuration="$locations/$branch.env"

  if [[ ! -f "$configuration" ]]; then
    echo "Create $branch"
    port=$(assign_port) || true
    if [[ -z "$port" ]]; then
      echo "Skip $branch"
      continue
    fi
    cat >"$configuration" <<EOF
# DIGEST=$digest
DAGSTER_LOCATION=$branch
IMAGE_TAG=$branch
GRPC_PORT=$port
IMAGE_REPOSITORY=${code_image_repository}
EOF
    systemctl enable --now "$unit@$branch.service"
  else
    current_digest=$(get_env_digest "$configuration")
    if [[ "$current_digest" != "$digest" ]]; then
      echo "Update $branch"
      if grep -q '^IMAGE_TAG=' "$configuration"; then
        sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$branch/" "$configuration"
      else
        echo "IMAGE_TAG=$branch" >>"$configuration"
      fi
      if grep -q '^IMAGE_REPOSITORY=' "$configuration"; then
        sed -i "s|^IMAGE_REPOSITORY=.*|IMAGE_REPOSITORY=${code_image_repository}|" "$configuration"
      else
        echo "IMAGE_REPOSITORY=${code_image_repository}" >>"$configuration"
      fi
      set_env_digest "$configuration" "$digest"
      if ! systemctl restart "$unit@$branch.service"; then
        systemctl enable --now "$unit@$branch.service"
      fi
    else
      echo "Keep $branch without change"
    fi
  fi

  if [[ -f "$configuration" ]]; then
    if grep -q '^DAGSTER_LOCATION=' "$configuration"; then
      sed -i "s/^DAGSTER_LOCATION=.*/DAGSTER_LOCATION=$branch/" "$configuration"
    else
      echo "DAGSTER_LOCATION=$branch" >>"$configuration"
    fi
  fi

  ensure_cloud_run_job "$branch"
done

mapfile -t managed_units < <(
  systemctl list-units --full --all "$unit@*.service" --no-legend 2>/dev/null | awk '{print $1}'
)

if [[ ${#managed_units[@]} -eq 0 ]]; then
  mapfile -t managed_units < <(
    systemctl list-unit-files "$unit@*.service" --no-legend 2>/dev/null | awk '{print $1}'
  )
fi

for unit in "${managed_units[@]}"; do
  [[ -n "$unit" ]] || continue
  branch="${unit#*@}"
  branch="${branch%.service}"
  if [[ -n "${active_digests[$branch]+x}" ]]; then
    continue
  fi
  echo "Delete unit $branch"
  systemctl stop "$unit" || true
  systemctl disable "$unit" || true
  systemctl reset-failed "$unit" || true
  if [[ -d "/etc/systemd/system/$unit.d" ]]; then
    rm -rf "/etc/systemd/system/$unit.d"
  fi
done
systemctl daemon-reload || true

for configuration in "$locations"/*.env; do
  [ -e "$configuration" ] || continue
  branch=$(basename "$configuration" .env)
  if [[ -n "${active_digests[$branch]+x}" ]]; then
    continue
  fi
  echo "Delete environment $branch"
  rm -f "$configuration"
done

if [[ "$secrets_changed" -eq 1 ]]; then
  restart_location_services
fi

cleanup_cloud_run_jobs

mapfile -t running_images < <(
  docker ps --format '{{.Image}}' --filter "name=$unit-"
)

declare -A active_images
for branch in "${!active_digests[@]}"; do
  active_images["${code_image_repository}:$branch"]=1
done

mapfile -t images < <(
  docker images "${code_image_repository}" --format '{{.Repository}}:{{.Tag}}' | grep -v ':<none>$'
)

for image in "${images[@]}"; do
  if printf '%s\n' "${running_images[@]}" | grep -qx "$image"; then
    continue
  fi
  if [[ -n "${active_images[$image]+x}" ]]; then
    continue
  fi
  echo "GC $image"
  docker rmi "$image" || true
done
update_workspace
echo "done"
