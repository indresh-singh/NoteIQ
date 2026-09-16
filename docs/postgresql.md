# Persistent database for NoteIQ

NoteIQ uses Azure Database for PostgreSQL Flexible Server in production. SQLite remains the
default for local development and tests. PostgreSQL keeps users, sessions, meetings,
transcripts, jobs, notifications and encrypted ClickUp connections across Container App
revisions and supports multiple replicas safely.

## 1. Create the database

The shortest setup uses the checked-in helper. It prompts for the password without displaying
it, creates the database and protected Container Apps secret, and configures the environment
variable:

```sh
bash scripts/create_database.sh
```

The equivalent manual commands are below. Choose a new password containing letters and numbers
and do not commit it.

```sh
export NOTEIQ_DB_PASSWORD='REPLACE_WITH_A_NEW_STRONG_PASSWORD'

az postgres flexible-server create \
  --resource-group noteiq \
  --name noteiq-pg-3941dbbc \
  --location uaenorth \
  --admin-user noteiqadmin \
  --admin-password "$NOTEIQ_DB_PASSWORD" \
  --sku-name Standard_B1ms \
  --tier Burstable \
  --version 16 \
  --storage-size 32 \
  --public-access 0.0.0.0

az postgres flexible-server db create \
  --resource-group noteiq \
  --server-name noteiq-pg-3941dbbc \
  --database-name noteiq
```

`0.0.0.0` permits connections from Azure services, while PostgreSQL still requires the
password and TLS. For a production customer deployment, use a VNet and private endpoint
instead of this prototype firewall setting.

## 2. Give Container Apps the connection string

```sh
export NOTEIQ_DB_URL="postgresql://noteiqadmin:${NOTEIQ_DB_PASSWORD}@noteiq-pg-3941dbbc.postgres.database.azure.com:5432/noteiq?sslmode=require"

az containerapp secret set \
  --name noteiq \
  --resource-group noteiq \
  --secrets noteiq-database-url="$NOTEIQ_DB_URL"

az containerapp update \
  --name noteiq \
  --resource-group noteiq \
  --set-env-vars NOTEIQ_DATABASE_URL=secretref:noteiq-database-url \
  --min-replicas 1 \
  --max-replicas 1
```

Keep one replica until the SQLite data has been migrated. After migration, PostgreSQL supports
more replicas.

## 3. Preserve the existing meetings and transcripts

The current Azure Files snapshot is `/data/noteiq.snapshot.sqlite3`. After deploying the new
image and configuring `NOTEIQ_DATABASE_URL`, run this once against the active replica:

```sh
az containerapp exec \
  --name noteiq \
  --resource-group noteiq \
  --command "python -m scripts.migrate_database --source /data/noteiq.snapshot.sqlite3"
```

The command refuses to run when PostgreSQL already contains a user, preventing an accidental
second import. If the old snapshot is at a different path, pass that path to `--source`.

## 4. Verify persistence

```sh
curl https://noteiq.salmontree-16ed39aa.uaenorth.azurecontainerapps.io/healthz

az containerapp logs show \
  --name noteiq \
  --resource-group noteiq \
  --type console \
  --tail 100
```

Deploy another revision and confirm that NoteIQ still shows the same meetings. Once verified,
the Azure Files SQLite snapshot and volume mount are no longer needed.

Microsoft references: [Container Apps with PostgreSQL](https://learn.microsoft.com/en-us/azure/developer/python/tutorial-deploy-python-web-app-azure-container-apps-02),
[PostgreSQL Flexible Server CLI](https://learn.microsoft.com/en-us/cli/azure/postgres/flexible-server),
and [PostgreSQL firewall rules](https://learn.microsoft.com/en-us/azure/postgresql/network/how-to-networking-servers-deployed-public-access-add-firewall-rules).
