#!/usr/bin/env bash
# Deploy NoteIQ to the production Azure Container App.
# Run without arguments: bash scripts/deploy_prod.sh
set -Eeuo pipefail

if (( $# != 0 )); then
  echo "Usage: bash scripts/deploy_prod.sh" >&2
  echo "Do not pass a tag; the script generates a unique tag automatically." >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export NOTEIQ_ENV_FILE="$script_dir/../.env.prod"
export NOTEIQ_EXPECTED_APP_ENV="prod"
exec bash "$script_dir/deploy_daio.sh"
