#!/usr/bin/env bash
# Shared Azure Container Apps deployment engine.
# Use deploy_dev.sh or deploy_prod.sh rather than invoking this file directly.
#
# Run from the repository root:
#   bash scripts/deploy_dev.sh
#   bash scripts/deploy_prod.sh
#
# Every run generates a unique immutable image tag and revision suffix.
#
# Application settings come only from the selected .env file. Secret values are
# stored as Container App secrets and are never printed.
set -Eeuo pipefail

trap 'status=$?; echo "Deployment failed at line $LINENO (exit $status)." >&2' ERR

native_path() {
  local path="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -u "$path"
  elif command -v wslpath >/dev/null 2>&1; then
    wslpath -u "$path"
  else
    printf '%s\n' "$path"
  fi
}

resolve_command() {
  command -v "$1" 2>/dev/null || command -v "$1.exe" 2>/dev/null
}

resolve_docker() {
  local candidate path
  # Prefer Docker Desktop's Windows client under WSL; it talks directly to the
  # Desktop engine even when this distro's Docker integration is disabled.
  for candidate in docker.exe docker; do
    path="$(command -v "$candidate" 2>/dev/null || true)"
    if [[ -n "$path" ]] && "$path" info >/dev/null 2>&1; then
      printf '%s\n' "$path"
      return
    fi
  done
  return 1
}

# WSL cannot invoke the Windows az.cmd launcher directly, so use the Python
# bundled with the official Azure CLI installation when az is not on PATH.
declare -a az_command
azure_cli="$(command -v az 2>/dev/null || true)"
if [[ -n "$azure_cli" ]]; then
  az_command=("$azure_cli")
else
  azure_cli_python="${NOTEIQ_AZURE_CLI_PYTHON:-C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe}"
  azure_cli_python="$(native_path "$azure_cli_python")"
  if [[ -x "$azure_cli_python" ]]; then
    az_command=("$azure_cli_python" -m azure.cli)
  else
    echo "Azure CLI is unavailable. Install it or set NOTEIQ_AZURE_CLI_PYTHON." >&2
    exit 3
  fi
fi

az() {
  "${az_command[@]}" "$@"
}

az_tsv() {
  az "$@" --output tsv | tr -d '\r'
}

uv_command="$(resolve_command uv || true)"
docker_command="$(resolve_docker || true)"
if [[ -z "$uv_command" ]]; then
  echo "Required command is unavailable: uv" >&2
  exit 4
fi
if [[ -z "$docker_command" ]]; then
  echo "No Docker client can reach Docker Desktop's Linux engine." >&2
  echo "Run 'docker info' in this shell or enable Docker Desktop WSL integration." >&2
  exit 5
fi

readonly env_file="${NOTEIQ_ENV_FILE:?Use scripts/deploy_dev.sh or scripts/deploy_prod.sh}"
readonly expected_app_env="${NOTEIQ_EXPECTED_APP_ENV:?Missing expected environment}"
if [[ ! -f "$env_file" ]]; then
  echo "Missing environment file: $env_file" >&2
  exit 2
fi

for name in \
  APP_ENV \
  NOTEIQ_DEPLOY_GRAPH_CLIENT_ID \
  NOTEIQ_DEPLOY_GRAPH_CLIENT_SECRET \
  NOTEIQ_DEPLOY_GRAPH_CLIENT_STATE \
  NOTEIQ_DEPLOY_SUBSCRIPTION_ID \
  NOTEIQ_DEPLOY_TENANT_ID \
  NOTEIQ_DEPLOY_TEAMS_APP_ID \
  NOTEIQ_DEPLOY_RESOURCE_GROUP \
  NOTEIQ_DEPLOY_CONTAINER_APP \
  NOTEIQ_DEPLOY_ACR_NAME \
  GRAPH_CLIENT_SECRET \
  GRAPH_CLIENT_STATE \
  PUBLIC_BASE_URL \
  NOTEIQ_DATABASE_URL \
  NOTEIQ_BACKUP_DATABASE \
  CLICKUP_CLIENT_ID \
  CLICKUP_CLIENT_SECRET \
  CLICKUP_TOKEN_KEY \
  PLANNER_GRAPH_VERSION \
  OPENROUTER_API_KEY \
  OPENROUTER_MODEL \
  OPENAI_API_KEY \
  OPENAI_MODEL \
  OPENAI_MIN_REQUEST_INTERVAL_SECONDS \
  NOTEIQ_ROLE \
  NOTEIQ_JOB_CONCURRENCY \
  NOTEIQ_SUBSCRIPTION_CONCURRENCY \
  NOTEIQ_MEETING_RETENTION_DAYS \
  NOTEIQ_LOG_LEVEL \
  MIN_REPLICAS \
  MAX_REPLICAS; do
  unset "$name"
done

# python-dotenv parses quoted values without evaluating shell syntax. NUL
# delimiters preserve spaces and other characters in secrets.
while IFS= read -r -d '' name && IFS= read -r -d '' value; do
  export "$name=$value"
done < <(
  "$uv_command" run python -c \
    'import os, sys; from dotenv import dotenv_values; values=dotenv_values(sys.argv[1], interpolate=False); [sys.stdout.buffer.write(k.encode()+b"\0"+(v or "").encode()+b"\0") for k,v in values.items()]' \
    "$env_file"
)

if (( $# != 0 )); then
  echo "This deployment command does not accept tags; a unique tag is generated automatically." >&2
  exit 2
fi

readonly app_env="${APP_ENV:-$expected_app_env}"
readonly subscription_id="${NOTEIQ_DEPLOY_SUBSCRIPTION_ID:?Missing NOTEIQ_DEPLOY_SUBSCRIPTION_ID in $env_file}"
readonly tenant_id="${NOTEIQ_DEPLOY_TENANT_ID:?Missing NOTEIQ_DEPLOY_TENANT_ID in $env_file}"
readonly graph_client_id="${NOTEIQ_DEPLOY_GRAPH_CLIENT_ID:?Missing NOTEIQ_DEPLOY_GRAPH_CLIENT_ID in $env_file}"
readonly teams_app_id="${NOTEIQ_DEPLOY_TEAMS_APP_ID:?Missing NOTEIQ_DEPLOY_TEAMS_APP_ID in $env_file}"
readonly graph_client_secret="${NOTEIQ_DEPLOY_GRAPH_CLIENT_SECRET:?Missing NOTEIQ_DEPLOY_GRAPH_CLIENT_SECRET in $env_file}"
readonly graph_client_state="${NOTEIQ_DEPLOY_GRAPH_CLIENT_STATE:?Missing NOTEIQ_DEPLOY_GRAPH_CLIENT_STATE in $env_file}"
readonly resource_group="${NOTEIQ_DEPLOY_RESOURCE_GROUP:?Missing NOTEIQ_DEPLOY_RESOURCE_GROUP in $env_file}"
readonly container_app="${NOTEIQ_DEPLOY_CONTAINER_APP:?Missing NOTEIQ_DEPLOY_CONTAINER_APP in $env_file}"
readonly acr_name="${NOTEIQ_DEPLOY_ACR_NAME:?Missing NOTEIQ_DEPLOY_ACR_NAME in $env_file}"
readonly acr_server="${acr_name}.azurecr.io"
readonly image_repository="noteiq"
readonly min_replicas="${MIN_REPLICAS:-1}"
readonly max_replicas="${MAX_REPLICAS:-1}"

# 7 + 12 + 7 + 4 = 30 characters: valid under the 32-character tag rule.
# Time and source revision aid traceability; the random suffix prevents a
# collision between concurrent deployments of the same commit.
timestamp="$(date -u +%y%m%d%H%M%S)"
commit="$(git rev-parse --short=7 HEAD 2>/dev/null || printf 'nogit00')"
nonce="$(od -An -N2 -tx1 /dev/urandom | tr -d '[:space:]')"
readonly tag="release${timestamp}${commit}${nonce}"

echo "Checking Docker and Azure access..."

if [[ "$app_env" != "$expected_app_env" ]]; then
  echo "$env_file must contain APP_ENV=$expected_app_env, not APP_ENV=$app_env." >&2
  exit 2
fi

if ! [[ "$min_replicas" =~ ^[0-9]+$ && "$max_replicas" =~ ^[0-9]+$ ]]; then
  echo "MIN_REPLICAS and MAX_REPLICAS must be nonnegative integers." >&2
  exit 5
fi
if (( min_replicas > max_replicas )); then
  echo "MIN_REPLICAS cannot exceed MAX_REPLICAS." >&2
  exit 5
fi

if ! az account show --output none >/dev/null 2>&1; then
  echo "Azure CLI is not signed in. Run: az login" >&2
  exit 6
fi

az account set --subscription "$subscription_id"
account_tenant="$(az_tsv account show --query tenantId)"
if [[ "$account_tenant" != "$tenant_id" ]]; then
  echo "Subscription $subscription_id belongs to tenant $account_tenant, not expected tenant $tenant_id." >&2
  echo "Run: az login --tenant $tenant_id" >&2
  exit 7
fi

declare -ar container_args=(
  --name "$container_app"
  --resource-group "$resource_group"
  --subscription "$subscription_id"
)

if ! az_tsv group show \
  --name "$resource_group" \
  --subscription "$subscription_id" \
  --query id >/dev/null; then
  echo "Azure access denied for resource group: $resource_group" >&2
  echo "Ask an Azure administrator for the deployment roles, then sign in again." >&2
  exit 15
fi

fqdn="$(az_tsv containerapp show "${container_args[@]}" \
  --query properties.configuration.ingress.fqdn)"
if [[ -z "$fqdn" ]]; then
  echo "Container App $container_app has no ingress FQDN." >&2
  exit 8
fi
readonly public_base_url="https://${fqdn}"
if [[ "${PUBLIC_BASE_URL%/}" != "$public_base_url" ]]; then
  echo "PUBLIC_BASE_URL in $env_file does not match $container_app ($public_base_url)." >&2
  exit 8
fi

readonly image="${acr_server}/${image_repository}:${tag}"
readonly revision_name="${container_app}--${tag}"

# A Container App revision is immutable. Reusing the image tag and revision
# suffix can leave the old healthy revision serving traffic even after Docker
# has pushed new bytes under that mutable tag. Check both places before the
# build, so this command cannot report an old revision as a new deployment.
if az containerapp revision show "${container_args[@]}" \
  --revision "$revision_name" \
  --output none >/dev/null 2>&1; then
  echo "Refusing to reuse existing Container App revision: $revision_name" >&2
  echo "Run the deployment script again to generate a different tag." >&2
  exit 12
fi

# The revision check is the deployment safety boundary. This ACR check also
# prevents a tag from being silently moved before it ever reaches this app.
# A missing repository is expected before the first deployment.
existing_tag="$(az_tsv acr repository show-tags \
  --name "$acr_name" \
  --repository "$image_repository" \
  --subscription "$subscription_id" \
  --query "[?@=='${tag}'] | [0]" 2>/dev/null || true)"
if [[ "$existing_tag" == "$tag" ]]; then
  echo "Refusing to reuse existing ACR image tag: $image" >&2
  echo "Run the deployment script again to generate a different tag." >&2
  exit 13
fi

echo "Generated deployment tag: $tag"
echo "Environment:   $app_env"
echo "Tenant:        $tenant_id"
echo "Subscription:  $subscription_id"
echo "Resource group: $resource_group"
echo "Container App:  $container_app"
echo "Public origin:  $public_base_url"
echo "Image:          $image"

env -u VIRTUAL_ENV \
APP_ENV="$app_env" \
AZURE_TENANT_ID="$tenant_id" \
GRAPH_CLIENT_ID="$graph_client_id" \
GRAPH_CLIENT_SECRET="package-build-only" \
GRAPH_CLIENT_STATE="package-build-only-client-state-00000000" \
PUBLIC_BASE_URL="$public_base_url" \
TEAMS_APP_ID="$teams_app_id" \
  "$uv_command" run python -m scripts.package_teams --app-id "$teams_app_id"
"$docker_command" build --platform linux/amd64 --tag "$image" .

az acr login \
  --name "$acr_name" \
  --subscription "$subscription_id" \
  --only-show-errors
"$docker_command" push "$image"

declare -a secret_values=(
  "graph-client-secret=$graph_client_secret"
  "graph-client-state=$graph_client_state"
)
declare -a update_env_vars=(
  "APP_ENV=$app_env"
  "AZURE_TENANT_ID=$tenant_id"
  "GRAPH_CLIENT_ID=$graph_client_id"
  "GRAPH_CLIENT_SECRET=secretref:graph-client-secret"
  "GRAPH_CLIENT_STATE=secretref:graph-client-state"
  "PUBLIC_BASE_URL=$public_base_url"
  "TEAMS_APP_ID=$teams_app_id"
)

add_secret() {
  local env_name="$1" secret_name="$2" value="${!1:-}"
  if [[ -n "$value" ]]; then
    secret_values+=("$secret_name=$value")
    update_env_vars+=("$env_name=secretref:$secret_name")
  fi
}

add_setting() {
  local name="$1" value="${!1:-}"
  if [[ -n "$value" ]]; then
    update_env_vars+=("$name=$value")
  fi
}

add_secret NOTEIQ_DATABASE_URL noteiq-database-url
add_secret CLICKUP_CLIENT_SECRET clickup-client-secret
add_secret CLICKUP_TOKEN_KEY clickup-token-key
add_secret OPENROUTER_API_KEY openrouter-api-key
add_secret OPENAI_API_KEY openai-api-key
for name in \
  CLICKUP_CLIENT_ID \
  PLANNER_GRAPH_VERSION \
  OPENROUTER_MODEL \
  OPENAI_MODEL \
  OPENAI_MIN_REQUEST_INTERVAL_SECONDS \
  NOTEIQ_ROLE \
  NOTEIQ_JOB_CONCURRENCY \
  NOTEIQ_SUBSCRIPTION_CONCURRENCY \
  NOTEIQ_MEETING_RETENTION_DAYS \
  NOTEIQ_BACKUP_DATABASE \
  NOTEIQ_LOG_LEVEL; do
  add_setting "$name"
done

az containerapp secret set "${container_args[@]}" \
  --secrets "${secret_values[@]}" \
  --only-show-errors \
  --output none

az containerapp revision set-mode "${container_args[@]}" \
  --mode single \
  --output none

az containerapp update "${container_args[@]}" \
  --image "$image" \
  --revision-suffix "$tag" \
  --min-replicas "$min_replicas" \
  --max-replicas "$max_replicas" \
  --set-env-vars "${update_env_vars[@]}" \
  --only-show-errors \
  --output none

health=""
for _ in {1..30}; do
  health="$(az_tsv containerapp revision show "${container_args[@]}" \
    --revision "$revision_name" \
    --query properties.healthState 2>/dev/null || true)"
  if [[ "$health" == "Healthy" ]]; then
    break
  fi
  if [[ "$health" == "Unhealthy" || "$health" == "Failed" ]]; then
    echo "Revision $revision_name reported health state: $health" >&2
    exit 10
  fi
  sleep 5
done

az containerapp revision list "${container_args[@]}" \
  --query '[].{Revision:name,Health:properties.healthState,Active:properties.active,Replicas:properties.replicas}' \
  --output table

if [[ "$health" != "Healthy" ]]; then
  echo "Timed out waiting for $revision_name to become Healthy (last state: ${health:-unknown})." >&2
  exit 11
fi

echo "Deployment completed: $revision_name is Healthy."
echo "Open: $public_base_url"
echo "Container App settings were loaded from $env_file."
