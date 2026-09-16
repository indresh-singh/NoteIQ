#!/usr/bin/env bash
# Create NoteIQ's persistent PostgreSQL database and connect the Container App.
set -euo pipefail

server="noteiq-pg-3941dbbc"
read -r -s -p "New PostgreSQL password (letters and numbers, 16+ characters): " db_password
echo
if [[ ! "$db_password" =~ ^[A-Za-z0-9]{16,128}$ ]]; then
  echo "Use 16-128 letters and numbers so the password is safe in the connection URL." >&2
  exit 1
fi

echo "Registering the Azure PostgreSQL resource provider..."
az provider register \
  --namespace Microsoft.DBforPostgreSQL \
  --wait \
  --output none

az postgres flexible-server create \
  --resource-group noteiq \
  --name "$server" \
  --location uaenorth \
  --admin-user noteiqadmin \
  --admin-password "$db_password" \
  --sku-name Standard_B1ms \
  --tier Burstable \
  --version 16 \
  --storage-size 32 \
  --public-access 0.0.0.0 \
  --output none

az postgres flexible-server db create \
  --resource-group noteiq \
  --server-name "$server" \
  --database-name noteiq \
  --output none

database_url="postgresql://noteiqadmin:${db_password}@${server}.postgres.database.azure.com:5432/noteiq?sslmode=require"
az containerapp secret set \
  --name noteiq \
  --resource-group noteiq \
  --secrets noteiq-database-url="$database_url" \
  --output none

az containerapp update \
  --name noteiq \
  --resource-group noteiq \
  --set-env-vars NOTEIQ_DATABASE_URL=secretref:noteiq-database-url \
  --min-replicas 1 \
  --max-replicas 1 \
  --output none

echo "Database created and connected. Deploy the PostgreSQL-capable image next."
