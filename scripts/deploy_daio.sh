#!/usr/bin/env bash
# Deploy NoteIQ to the DAIO development Azure Container App.
#
# Run from the repository root:
#   bash scripts/deploy_daio.sh [UNIQUE_TAG]
#
# Tags are immutable deployment identities: this script rejects a tag that is
# already present in ACR or already names a Container App revision.
#
# The Graph and OpenAI API secrets must already be configured on the Container
# App as secret references. This script intentionally never accepts or prints
# either value.
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

readonly subscription_id="02fcef19-1aef-4374-8f4e-1d126a361dc8"
readonly tenant_id="f0869253-be00-4a37-9c77-37742cb15c38"
readonly graph_client_id="d6fc5dd5-578e-4ebd-a962-e16c84bd6be7"
readonly graph_client_id_secret_name="graph-client-id"
readonly openai_api_key_secret_name="openai-api-key"
readonly teams_app_id="54bbd41a-9e11-4578-a658-7ec30577d393"
readonly resource_group="rg-uaen-dev-daio-infra-001"
readonly container_app="dev-daio-noteiq"
readonly acr_name="uaedaioinfradev"
readonly acr_server="${acr_name}.azurecr.io"
readonly image_repository="noteiq"
readonly min_replicas="${MIN_REPLICAS:-1}"
readonly max_replicas="${MAX_REPLICAS:-10}"
declare -ar container_args=(
  --name "$container_app"
  --resource-group "$resource_group"
  --subscription "$subscription_id"
)

tag="${1:-}"
generated_tag=false
if [[ -z "$tag" ]]; then
  # 7 + 12 + 7 + 4 = 30 characters: valid under the 32-character tag rule.
  # The timestamp and source revision make deployments easy to trace; the
  # random suffix prevents a collision between concurrent deployments of the
  # same commit in the same second.
  timestamp="$(date -u +%y%m%d%H%M%S)"
  commit="$(git rev-parse --short=7 HEAD 2>/dev/null || printf 'nogit00')"
  nonce="$(od -An -N2 -tx1 /dev/urandom | tr -d '[:space:]')"
  tag="release${timestamp}${commit}${nonce}"
  generated_tag=true
fi
if [[ ! "$tag" =~ ^[a-z][a-z0-9]{0,31}$ ]]; then
  echo "Usage: bash scripts/deploy_daio.sh [UNIQUE_TAG]" >&2
  echo "The tag must start with a lowercase letter and contain at most 32 lowercase letters/numbers." >&2
  exit 2
fi

echo "Checking Docker and Azure access..."

if ! [[ "$min_replicas" =~ ^[0-9]+$ && "$max_replicas" =~ ^[0-9]+$ ]]; then
  echo "MIN_REPLICAS and MAX_REPLICAS must be nonnegative integers." >&2
  exit 5
fi
if (( min_replicas > max_replicas )); then
  echo "MIN_REPLICAS cannot exceed MAX_REPLICAS." >&2
  exit 5
fi

if ! az account show --output none >/dev/null 2>&1; then
  echo "Azure CLI is not signed in. Run: az login --tenant $tenant_id" >&2
  exit 6
fi

az account set --subscription "$subscription_id"
account_tenant="$(az_tsv account show --query tenantId)"
if [[ "$account_tenant" != "$tenant_id" ]]; then
  echo "Subscription $subscription_id belongs to tenant $account_tenant, not expected tenant $tenant_id." >&2
  echo "Run: az login --tenant $tenant_id" >&2
  exit 7
fi

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

graph_secret_ref="$(az_tsv containerapp show "${container_args[@]}" \
  --query "properties.template.containers[0].env[?name=='GRAPH_CLIENT_SECRET'] | [0].secretRef")"
if [[ -z "$graph_secret_ref" ]]; then
  echo "GRAPH_CLIENT_SECRET is not configured as a Container App secret reference." >&2
  echo "Configure it securely in Azure before deploying; do not put the secret in this script." >&2
  exit 9
fi

openai_secret_ref="$(az_tsv containerapp show "${container_args[@]}" \
  --query "properties.template.containers[0].env[?name=='OPENAI_API_KEY'] | [0].secretRef")"
if [[ -z "$openai_secret_ref" ]]; then
  echo "OPENAI_API_KEY is not configured as a Container App secret reference." >&2
  echo "Create the '$openai_api_key_secret_name' secret and bind OPENAI_API_KEY to it before deploying." >&2
  echo "Do not put the API key in this script, an image layer, or a plain environment value." >&2
  exit 14
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
  echo "Choose a new UNIQUE_TAG, for example: release0922a" >&2
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
  echo "Choose a new UNIQUE_TAG, for example: release0922a" >&2
  exit 13
fi

if [[ "$generated_tag" == true ]]; then
  echo "Generated deployment tag: $tag"
fi
echo "Tenant:        $tenant_id"
echo "Subscription:  $subscription_id"
echo "Resource group: $resource_group"
echo "Container App:  $container_app"
echo "Public origin:  $public_base_url"
echo "Image:          $image"

env -u VIRTUAL_ENV \
AZURE_TENANT_ID="$tenant_id" \
GRAPH_CLIENT_ID="$graph_client_id" \
PUBLIC_BASE_URL="$public_base_url" \
TEAMS_APP_ID="$teams_app_id" \
  "$uv_command" run python -m scripts.package_teams --app-id "$teams_app_id"
"$docker_command" build --platform linux/amd64 --tag "$image" .

az acr login \
  --name "$acr_name" \
  --subscription "$subscription_id" \
  --only-show-errors
"$docker_command" push "$image"

# Preserve the Container App's existing secret-reference convention. A client
# ID is not confidential, but keeping it in the existing named secret avoids an
# unnecessary environment-shape change between revisions.
az containerapp secret set "${container_args[@]}" \
  --secrets "${graph_client_id_secret_name}=${graph_client_id}" \
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
  --set-env-vars \
    "AZURE_TENANT_ID=$tenant_id" \
    "GRAPH_CLIENT_ID=secretref:$graph_client_id_secret_name" \
    "OPENAI_API_KEY=secretref:$openai_secret_ref" \
    "OPENAI_MODEL=gpt-5.6-luna" \
    "OPENAI_MIN_REQUEST_INTERVAL_SECONDS=30" \
    "PUBLIC_BASE_URL=$public_base_url" \
    "TEAMS_APP_ID=$teams_app_id" \
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
echo "PostgreSQL data and all existing Container App secret references were preserved."
