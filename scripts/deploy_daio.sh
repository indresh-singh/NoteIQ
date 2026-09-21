#!/usr/bin/env bash
# Deploy NoteIQ to the DAIO development Azure Container App.
#
# Run from the repository root:
#   bash scripts/deploy_daio.sh [UNIQUE_TAG]
#
# Tags are immutable deployment identities: this script rejects a tag that is
# already present in ACR or already names a Container App revision.
#
# The Graph client secret must already be configured on the Container App as a
# secret reference. This script intentionally never accepts or prints it.
set -Eeuo pipefail

# Prefer the standard Azure CLI launcher. On this workstation Codex installed
# the CLI in a local Python environment, so support that location as a fallback.
# Set NOTEIQ_AZURE_CLI_PYTHON to use a different Python executable that has the
# azure-cli package installed.
declare -a az_command
if command -v az >/dev/null 2>&1; then
  az_command=(az)
else
  azure_cli_python="${NOTEIQ_AZURE_CLI_PYTHON:-}"
  if [[ -z "$azure_cli_python" && -n "${LOCALAPPDATA:-}" ]]; then
    local_app_data="$LOCALAPPDATA"
    if command -v cygpath >/dev/null 2>&1; then
      local_app_data="$(cygpath --unix "$LOCALAPPDATA")"
    fi
    candidate_python="$local_app_data/Temp/noteiq-azure-tools/azure-cli/Scripts/python.exe"
    if [[ -x "$candidate_python" ]]; then
      azure_cli_python="$candidate_python"
    fi
  fi
  if [[ -n "$azure_cli_python" && -x "$azure_cli_python" ]]; then
    az_command=("$azure_cli_python" -m azure.cli)
  else
    echo "Azure CLI is unavailable. Install the Azure CLI, or set NOTEIQ_AZURE_CLI_PYTHON to its Python executable." >&2
    exit 3
  fi
fi

az() {
  "${az_command[@]}" "$@"
}

readonly subscription_id="02fcef19-1aef-4374-8f4e-1d126a361dc8"
readonly tenant_id="f0869253-be00-4a37-9c77-37742cb15c38"
readonly graph_client_id="d6fc5dd5-578e-4ebd-a962-e16c84bd6be7"
readonly graph_client_id_secret_name="graph-client-id"
readonly teams_app_id="54bbd41a-9e11-4578-a658-7ec30577d393"
readonly resource_group="rg-uaen-dev-daio-infra-001"
readonly container_app="dev-daio-noteiq"
readonly acr_name="uaedaioinfradev"
readonly acr_server="${acr_name}.azurecr.io"
readonly image_repository="noteiq"
readonly min_replicas="${MIN_REPLICAS:-1}"
readonly max_replicas="${MAX_REPLICAS:-10}"

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

for command in docker uv; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Required command is unavailable: $command" >&2
    exit 4
  fi
done

if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop's Linux engine is unavailable." >&2
  echo "Start Docker Desktop, wait until it reports that the engine is running, then rerun this command." >&2
  exit 5
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
  echo "Azure CLI is not signed in. Run: az login --tenant $tenant_id" >&2
  exit 6
fi

az account set --subscription "$subscription_id"
account_tenant="$(az account show --query tenantId --output tsv)"
if [[ "$account_tenant" != "$tenant_id" ]]; then
  echo "Subscription $subscription_id belongs to tenant $account_tenant, not expected tenant $tenant_id." >&2
  echo "Run: az login --tenant $tenant_id" >&2
  exit 7
fi

az group show \
  --name "$resource_group" \
  --subscription "$subscription_id" \
  --output none

fqdn="$(az containerapp show \
  --name "$container_app" \
  --resource-group "$resource_group" \
  --subscription "$subscription_id" \
  --query properties.configuration.ingress.fqdn \
  --output tsv)"
if [[ -z "$fqdn" ]]; then
  echo "Container App $container_app has no ingress FQDN." >&2
  exit 8
fi
readonly public_base_url="https://${fqdn}"

graph_secret_ref="$(az containerapp show \
  --name "$container_app" \
  --resource-group "$resource_group" \
  --subscription "$subscription_id" \
  --query "properties.template.containers[0].env[?name=='GRAPH_CLIENT_SECRET'] | [0].secretRef" \
  --output tsv)"
if [[ -z "$graph_secret_ref" ]]; then
  echo "GRAPH_CLIENT_SECRET is not configured as a Container App secret reference." >&2
  echo "Configure it securely in Azure before deploying; do not put the secret in this script." >&2
  exit 9
fi

readonly image="${acr_server}/${image_repository}:${tag}"
readonly revision_name="${container_app}--${tag}"

# A Container App revision is immutable. Reusing the image tag and revision
# suffix can leave the old healthy revision serving traffic even after Docker
# has pushed new bytes under that mutable tag. Check both places before the
# build, so this command cannot report an old revision as a new deployment.
if az containerapp revision show \
  --name "$container_app" \
  --resource-group "$resource_group" \
  --subscription "$subscription_id" \
  --revision "$revision_name" \
  --output none >/dev/null 2>&1; then
  echo "Refusing to reuse existing Container App revision: $revision_name" >&2
  echo "Choose a new UNIQUE_TAG, for example: release0922a" >&2
  exit 12
fi

# The revision check is the deployment safety boundary. This ACR check also
# prevents a tag from being silently moved before it ever reaches this app.
# A missing repository is expected before the first deployment.
existing_tag="$(az acr repository show-tags \
  --name "$acr_name" \
  --repository "$image_repository" \
  --subscription "$subscription_id" \
  --query "[?@=='${tag}'] | [0]" \
  --output tsv 2>/dev/null || true)"
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
  uv run python -m scripts.package_teams --app-id "$teams_app_id"
docker build --platform linux/amd64 --tag "$image" .

az acr login \
  --name "$acr_name" \
  --subscription "$subscription_id" \
  --only-show-errors
docker push "$image"

# Preserve the Container App's existing secret-reference convention. A client
# ID is not confidential, but keeping it in the existing named secret avoids an
# unnecessary environment-shape change between revisions.
az containerapp secret set \
  --name "$container_app" \
  --resource-group "$resource_group" \
  --subscription "$subscription_id" \
  --secrets "${graph_client_id_secret_name}=${graph_client_id}" \
  --only-show-errors \
  --output none

az containerapp revision set-mode \
  --name "$container_app" \
  --resource-group "$resource_group" \
  --subscription "$subscription_id" \
  --mode single \
  --output none

az containerapp update \
  --name "$container_app" \
  --resource-group "$resource_group" \
  --subscription "$subscription_id" \
  --image "$image" \
  --revision-suffix "$tag" \
  --min-replicas "$min_replicas" \
  --max-replicas "$max_replicas" \
  --set-env-vars \
    "AZURE_TENANT_ID=$tenant_id" \
    "GRAPH_CLIENT_ID=secretref:$graph_client_id_secret_name" \
    "PUBLIC_BASE_URL=$public_base_url" \
    "TEAMS_APP_ID=$teams_app_id" \
  --only-show-errors \
  --output none

health=""
for _ in {1..30}; do
  health="$(az containerapp revision show \
    --name "$container_app" \
    --resource-group "$resource_group" \
    --subscription "$subscription_id" \
    --revision "$revision_name" \
    --query properties.healthState \
    --output tsv 2>/dev/null || true)"
  if [[ "$health" == "Healthy" ]]; then
    break
  fi
  if [[ "$health" == "Unhealthy" || "$health" == "Failed" ]]; then
    echo "Revision $revision_name reported health state: $health" >&2
    exit 10
  fi
  sleep 5
done

az containerapp revision list \
  --name "$container_app" \
  --resource-group "$resource_group" \
  --subscription "$subscription_id" \
  --query '[].{Revision:name,Health:properties.healthState,Active:properties.active,Replicas:properties.replicas}' \
  --output table

if [[ "$health" != "Healthy" ]]; then
  echo "Timed out waiting for $revision_name to become Healthy (last state: ${health:-unknown})." >&2
  exit 11
fi

echo "Deployment completed: $revision_name is Healthy."
echo "Open: $public_base_url"
echo "PostgreSQL data and all existing Container App secret references were preserved."
