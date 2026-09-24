#!/usr/bin/env bash
# Deploy NoteIQ to the shared DEV/TEST Azure Container App.
# Run without arguments: bash scripts/deploy_dev.sh
set -Eeuo pipefail

if (( $# != 0 )); then
  echo "Usage: bash scripts/deploy_dev.sh" >&2
  echo "Do not pass a tag; the script generates a unique tag automatically." >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export NOTEIQ_ENV_FILE="$script_dir/../.env.dev"
export NOTEIQ_EXPECTED_APP_ENV="dev"
exec bash "$script_dir/deploy_daio.sh"
