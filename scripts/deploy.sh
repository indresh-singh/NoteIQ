#!/usr/bin/env bash
# Run from the repository root: bash scripts/deploy.sh UNIQUE_TAG
set -euo pipefail
tag="${1:?Pass a new lowercase image tag, for example demo0914a}"
if [[ ! "$tag" =~ ^[a-z][a-z0-9]*$ ]]; then
  echo 'Use a lowercase letter followed by lowercase letters/numbers.' >&2
  exit 1
fi
image="ca7403cf3012acr.azurecr.io/noteiq:$tag"
uv run python -m scripts.package_teams
docker build --platform linux/amd64 -t "$image" .
az acr login --name ca7403cf3012acr
docker push "$image"
az containerapp revision set-mode -n noteiq -g noteiq --mode single --output none
az containerapp update -n noteiq -g noteiq --image "$image" \
  --revision-suffix "$tag" --min-replicas 1 --max-replicas 1 --output none
az containerapp revision list -n noteiq -g noteiq \
  --query '[].{Revision:name,Health:properties.healthState,Active:properties.active}' -o table
echo 'Wait for the new revision to be Healthy. Single mode switches traffic after readiness.'
echo 'Data remains in PostgreSQL when NOTEIQ_DATABASE_URL is configured.'
