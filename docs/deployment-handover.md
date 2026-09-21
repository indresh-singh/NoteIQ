# NoteIQ deployment handover

This document is the operational handover for NoteIQ. It describes the deployed
Teams personal app, its Azure runtime, Microsoft 365 configuration and release
process.

## What NoteIQ is

NoteIQ is a Python/FastAPI Teams personal tab. It receives Microsoft Graph
webhooks for Teams meeting transcripts and Copilot meeting insights, saves the
results, and sends Teams Activity notifications to the organizer.

It does **not** use Azure Bot Service, Azure Functions or Azure App Service.

```text
Teams personal tab
        |
        v
Azure Container App (FastAPI + background worker)
        |                         |
        v                         v
Microsoft Graph               Azure PostgreSQL
        |
        v
Teams transcripts, Copilot insights and Activity notifications
```

## Existing Azure environment

| Item | Value |
|---|---|
| Azure region | UAE North |
| Resource group | `noteiq` |
| Container App | `noteiq` |
| Container Apps environment | `noteiq-uae` |
| Public application URL | `https://noteiq.salmontree-16ed39aa.uaenorth.azurecontainerapps.io` |
| Container Registry | `ca7403cf3012acr.azurecr.io` |
| PostgreSQL server | `noteiq-pg-3941dbbc.postgres.database.azure.com` |
| PostgreSQL database | `noteiq` |
| Log Analytics workspace | `workspace-noteiqzmyi` |

Supporting/legacy resources include `noteiqstorage` and its Azure Files SQLite
snapshot. Keep them until the PostgreSQL migration is verified. Do not remove
them during a release.

## Required Azure resources

The production deployment needs:

| Azure resource | Purpose |
|---|---|
| Azure Container Registry | Stores versioned NoteIQ container images. |
| Azure Container Apps environment | Hosts the application and worker. |
| Azure Container App | Runs the FastAPI process on port `8000`. |
| Azure Database for PostgreSQL Flexible Server | Persistent application data. |
| Log Analytics workspace | Application, revision and platform logs. |
| Key Vault, recommended | Central storage for production secrets. |

The following providers must be registered on the subscription:

```sh
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.ContainerRegistry
az provider register --namespace Microsoft.DBforPostgreSQL
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.Storage
```

## Container App settings

| Setting | Required value |
|---|---|
| Ingress | External |
| Target port | `8000` |
| Health endpoint | `/healthz` |
| Revision mode | `Single` |
| Minimum replicas | `1` |
| Maximum replicas | `1` initially |
| Image platform | `linux/amd64` |

The health endpoint must return:

```json
{"status":"ok"}
```

Keep one replica until PostgreSQL is active and the legacy SQLite data has been
migrated. PostgreSQL supports multiple replicas safely; SQLite on an Azure Files
mount does not.

## Runtime configuration

Set these normal Container App environment variables:

| Variable | Value |
|---|---|
| `AZURE_TENANT_ID` | See `.env` — this is the single source of truth; do not duplicate the value in docs |
| `GRAPH_CLIENT_ID` | See `.env` — this is the single source of truth; do not duplicate the value in docs |
| `TEAMS_APP_ID` | Stable Teams package ID; normally the same as `GRAPH_CLIENT_ID` |
| `PUBLIC_BASE_URL` | `https://noteiq.salmontree-16ed39aa.uaenorth.azurecontainerapps.io` |
| `CLICKUP_CLIENT_ID` | Optional ClickUp OAuth client ID |
| `AI_PROVIDER` | `copilot` (default) or `openrouter` — selects which service generates meeting summaries and action items |
| `OPENROUTER_MODEL` | Required when `AI_PROVIDER=openrouter`: the OpenRouter model id, e.g. `openai/gpt-4o-mini` |
| `NOTEIQ_ROLE` | `all` (default) runs the web tier and the background worker in one process. `web` and `worker` split them into separate Container Apps; see [scaling](scaling.md) |
| `NOTEIQ_JOB_CONCURRENCY` | How many queued jobs the worker runs at once. Default `4`, maximum `32` |
| `NOTEIQ_MEETING_RETENTION_DAYS` | Optional. Unset means saved meetings are kept until the user disconnects. Setting it deletes meetings and their transcripts once they are older than this many days (minimum `7`) |

Set protected values as Container App secrets and reference them from variables:

| Secret name | Environment-variable reference | Purpose |
|---|---|---|
| `graph-client-secret` | `GRAPH_CLIENT_SECRET=secretref:graph-client-secret` | Entra confidential-client secret |
| `graph-client-state` | `GRAPH_CLIENT_STATE=secretref:graph-client-state` | Graph webhook validation secret |
| `noteiq-database-url` | `NOTEIQ_DATABASE_URL=secretref:noteiq-database-url` | PostgreSQL connection string |
| `clickup-client-secret` | `CLICKUP_CLIENT_SECRET=secretref:clickup-client-secret` | Optional ClickUp OAuth secret |
| `clickup-token-key` | `CLICKUP_TOKEN_KEY=secretref:clickup-token-key` | Optional Fernet key for encrypted ClickUp tokens |
| `openrouter-api-key` | `OPENROUTER_API_KEY=secretref:openrouter-api-key` | Required when `AI_PROVIDER=openrouter` |

The PostgreSQL URL format is:

```text
postgresql://noteiqadmin:PASSWORD@noteiq-pg-3941dbbc.postgres.database.azure.com:5432/noteiq?sslmode=require
```

Never put actual secret values in source control, Docker build arguments, the
Teams manifest or deployment scripts. Rotate any secret that was previously
entered as a plain Container App environment value or shared in terminal output.

## Database migration

The PostgreSQL server already exists. The remaining task is to configure
`NOTEIQ_DATABASE_URL`, deploy a PostgreSQL-capable image, and migrate the
existing SQLite snapshot once.

```sh
az containerapp exec \
  --name noteiq \
  --resource-group noteiq \
  --command "python -m scripts.migrate_database --source /data/noteiq.snapshot.sqlite3"
```

Then deploy one new revision and verify that the same users, meetings,
transcripts and ClickUp connections remain after the new revision is healthy.
Only then retire `NOTEIQ_DATABASE`, `NOTEIQ_BACKUP_DATABASE` and the Azure Files
volume.

## Microsoft Entra configuration

Use the existing single-tenant app registration:

| Item | Value |
|---|---|
| Application/client ID | See `.env`'s `GRAPH_CLIENT_ID` — single source of truth |
| Tenant ID | See `.env`'s `AZURE_TENANT_ID` — single source of truth |
| Supported accounts | Accounts in this organizational directory only |

Configure the following Web redirect URI:

```text
https://noteiq.salmontree-16ed39aa.uaenorth.azurecontainerapps.io/auth/callback
```

Required Microsoft Graph permissions:

| Type | Permission | Reason |
|---|---|---|
| Delegated | `User.Read` | Work-account sign-in and profile lookup. |
| Application | `OnlineMeetings.Read.All` | Validates the organizer and gets meeting details. |
| Application | `OnlineMeetingTranscript.Read.All` | Retrieves transcript updates and content. |
| Application | `OnlineMeetingAiInsight.Read.All` | Retrieves Copilot meeting insights. |

An Entra administrator must grant tenant-wide admin consent for the application
permissions. The current implementation does not need Calendar or Chat Graph
permissions.

## Teams configuration

Generate the package from the application configuration:

```sh
uv run python -m scripts.package_teams
```

Upload `dist/noteiq-teams.zip` through Teams Admin Center or allow the pilot
user to upload it as a custom app. Preserve the Teams app ID when publishing an
update, otherwise an installed app is treated as a different application.

The Teams manifest declares the resource-specific permission
`TeamsActivity.Send.User`. It is required for transcript-ready and
insights-ready entries in Teams Activity. It is declared in the Teams package,
not added in the Entra API-permissions screen.

The user must install the current package personally in Teams and accept the
permission. Browser-only use does not install the Teams Activity capability.

## Teams application access policy

The Entra permissions do not by themselves authorize the app to access a
meeting organizer's transcript or Copilot insights. A Teams administrator must
apply an application access policy to every organizer the tenant wants NoteIQ to
monitor.

Use the repository helper:

```powershell
pwsh ./scripts/grant_access.ps1
```

Use the NoteIQ Entra client ID shown above. Policy propagation can take up to 30
minutes. This is a Microsoft 365 tenant administration step, separate from
Azure deployment.

Each monitored organizer also needs a Teams license and Microsoft 365 Copilot
license. NoteIQ cannot make Copilot insights appear sooner or create missing
insights.

## Public endpoints and network access

Microsoft Graph must reach these public HTTPS endpoints:

```text
GET  /healthz
POST /api/graph/notifications
POST /api/graph/lifecycle
GET  /auth/callback
GET  /clickup/callback                 # only when ClickUp is enabled
```

Allow outbound HTTPS to:

```text
login.microsoftonline.com
graph.microsoft.com
api.clickup.com                        # only when ClickUp is enabled
```

Allow the Container App to connect to PostgreSQL on TCP `5432`. The current
prototype uses PostgreSQL public access with Azure-services firewall access.
For a customer production deployment, use VNet integration, private endpoint
and private DNS before disabling public database access.

## Release procedure

Run from the repository root. Each release requires a new lowercase tag.

```sh
bash scripts/deploy.sh release20260916a
```

The script:

1. Regenerates `dist/noteiq-teams.zip`.
2. Builds the Docker image for `linux/amd64`.
3. Pushes it to Azure Container Registry.
4. Creates a new Container Apps revision in single-revision mode.

The equivalent manual release is:

```sh
TAG=release20260916a
IMAGE=ca7403cf3012acr.azurecr.io/noteiq:$TAG

docker build --platform linux/amd64 -t "$IMAGE" .
az acr login --name ca7403cf3012acr
docker push "$IMAGE"

az containerapp revision set-mode --name noteiq --resource-group noteiq --mode single
az containerapp update \
  --name noteiq \
  --resource-group noteiq \
  --image "$IMAGE" \
  --revision-suffix "$TAG" \
  --min-replicas 1 \
  --max-replicas 1
```

Wait for the revision to become healthy before considering the release complete:

```sh
az containerapp revision list \
  --name noteiq \
  --resource-group noteiq \
  --query '[].{Revision:name,Active:properties.active,Health:properties.healthState}' \
  --output table

curl -fsS https://noteiq.salmontree-16ed39aa.uaenorth.azurecontainerapps.io/healthz
```

Regenerate and upload the Teams ZIP only when the Teams manifest, icons, tab
configuration or requested Teams permissions changed. A backend-only release
does not require re-uploading the package.

## Monitoring and operational checks

View application logs:

```sh
az containerapp logs show \
  --name noteiq \
  --resource-group noteiq \
  --type console \
  --tail 100
```

Track:

- unhealthy revisions, restarts and failed health checks;
- Graph webhook, subscription and Activity-notification HTTP failures;
- PostgreSQL storage, CPU and connections;
- Entra client-secret expiry;
- failed ClickUp task delivery, if ClickUp is enabled.

## Required roles

| Person or identity | Suggested access |
|---|---|
| CI/CD identity | `AcrPush` on the registry and Container App Contributor on the app/resource group. |
| Container App managed identity, if enabled | `AcrPull` on the registry. |
| DevOps engineer | Contributor on the `noteiq` resource group. |
| Operations team | Log Analytics Reader. |
| Database administrator | PostgreSQL Flexible Server Contributor. |
| Secret administrator | Key Vault Secrets Officer, if Key Vault is adopted. |
| Microsoft 365 administrator | Entra admin consent, Teams app policy and Teams application access policy. |

Azure RBAC is insufficient for Microsoft 365 tenant administration. The Microsoft
365 administrator needs an appropriate Entra/Teams administrative role to grant
consent, upload/approve the app, and apply the Teams application access policy.

## Handover validation

The handover is complete when all of the following pass:

1. `/healthz` returns `{"status":"ok"}` from the public URL.
2. The application starts with `NOTEIQ_DATABASE_URL` and retains content after a revision deployment.
3. Microsoft sign-in succeeds using the registered redirect URI.
4. The Teams ZIP installs and opens the personal Meetings tab.
5. The organizer is covered by the Teams application access policy.
6. Graph transcript and insight subscriptions are created after the user connects.
7. A transcribed Teams meeting appears in NoteIQ.
8. Copilot insights appear when Microsoft publishes them.
9. Transcript-ready and insights-ready Activity notifications arrive in Teams.
10. ClickUp connects and creates tasks when its optional settings are configured.

For detailed setup instructions, see [setup.md](setup.md),
[container-apps.md](container-apps.md), [postgresql.md](postgresql.md), and
[transcripts-and-messages.md](transcripts-and-messages.md).
