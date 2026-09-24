# Deploy NoteIQ to Azure Container Apps

Azure Container Apps replaces the Mac, tunnel and gateway. These instructions use
**Consumption** in **UAE North**. Configure [Azure Database for PostgreSQL](postgresql.md)
before relying on persistent data or deploying multiple replicas.
The application deploys as a single replica running both the web tier and the background
worker; [scaling](scaling.md) covers the capacity that gives you and how to separate the
two when you outgrow it.
The subscription must allow Container Apps and Azure Container Registry resources.

## 1. Build and deploy the container

Install the Azure CLI on the Mac, then open Terminal in this project directory. Sign in and
prepare Container Apps:

```sh
az login
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az group create --name noteiq-demo --location uaenorth
az containerapp env create --name noteiq-uae --resource-group noteiq-demo --location uaenorth
```

Get the environment domain:

```sh
az containerapp env show --name noteiq-uae --resource-group noteiq-demo --query properties.defaultDomain --output tsv
```

The public URL will be `https://noteiq.DOMAIN-FROM-THE-COMMAND`. Keep it for the next steps.
Build the checked-in `Dockerfile` in Azure and create the application. Replace all four
placeholders first. The placeholder secret only lets the first revision start; it is replaced
with a protected Container Apps secret in step 2.

```sh
az containerapp up --name noteiq --resource-group noteiq-demo --environment noteiq-uae --location uaenorth --source . --ingress external --target-port 8000 --env-vars AZURE_TENANT_ID=YOUR-TENANT-ID GRAPH_CLIENT_ID=YOUR-CLIENT-ID GRAPH_CLIENT_SECRET=not-configured-yet GRAPH_CLIENT_STATE=not-configured-yet-0000000000000000 PUBLIC_BASE_URL=https://noteiq.DOMAIN-FROM-THE-COMMAND NOTEIQ_DATABASE=/tmp/noteiq.sqlite3
```

`az containerapp up --source .` uses the Dockerfile, creates an Azure Container Registry when
needed, builds the image and deploys it. You do not need Docker Desktop for this path.

### If the trial blocks ACR Tasks

If the command returns `TasksOperationsNotAllowed`, the registry was created but the remote
builder is unavailable to the trial. Build on the Mac and push the image directly instead.
Replace the login server if the first command returns a different value:

```sh
az acr show --name ca7403cf3012acr --query loginServer --output tsv
az acr login --name ca7403cf3012acr
docker build --platform linux/amd64 --tag ca7403cf3012acr.azurecr.io/noteiq:v1.3.0 .
docker push ca7403cf3012acr.azurecr.io/noteiq:v1.3.0
az containerapp registry set --name noteiq --resource-group noteiq --server ca7403cf3012acr.azurecr.io
az containerapp revision set-mode --name noteiq --resource-group noteiq --mode single
az containerapp update --name noteiq --resource-group noteiq --image ca7403cf3012acr.azurecr.io/noteiq:v1.3.0 --revision-suffix v130 --min-replicas 1 --max-replicas 1 --set-env-vars NOTEIQ_DATABASE=/tmp/noteiq.sqlite3
az containerapp revision show --name noteiq --resource-group noteiq --revision noteiq--v130 --query "{Active:properties.active,Health:properties.healthState,Replicas:properties.replicas}" --output table
az containerapp ingress traffic set --name noteiq --resource-group noteiq --revision-weight noteiq--v130=100
```

Single mode keeps the old healthy revision until the new one is ready. Two active revisions
during a failed rollout do not mean the replica limit was ignored: that limit is per revision.
The explicit database override is necessary; an existing Azure environment variable overrides
the Dockerfile default. If the new revision is unhealthy, inspect its logs. Use a new image tag
and revision suffix for every release. `bash scripts/deploy_prod.sh` performs these steps
for the existing `noteiq` resource group and generates both values automatically. Use
`bash scripts/deploy_dev.sh` for the shared DEV/TEST DAIO resources.

This path uses the developer's current Azure login to push. Container Apps stores inferred ACR
pull credentials when the registry is configured. If the final registry command cannot infer
credentials, enable the ACR admin user temporarily in the portal, select the registry from the
Container App's registry settings, then disable the admin user after switching to managed
identity.

## 2. Add secrets and runtime settings

In **Azure Portal → Container Apps → noteiq → Settings → Secrets**, add:

| Secret name | Value |
|---|---|
| `graph-client-secret` | The Entra client secret **Value** |
| `graph-client-state` | A new random string of at least 32 characters |
| `openai-api-key` | A rotated OpenAI project API key |

Then open **Application → Revisions and replicas → Create new revision**. In the container's
environment variables, set:

| Name | Source | Value |
|---|---|---|
| `AZURE_TENANT_ID` | Manual | Your tenant ID |
| `GRAPH_CLIENT_ID` | Manual | Your `note-iq` client ID |
| `GRAPH_CLIENT_SECRET` | Secret reference | `graph-client-secret` |
| `GRAPH_CLIENT_STATE` | Secret reference | `graph-client-state` |
| `OPENAI_API_KEY` | Secret reference | `openai-api-key` |
| `OPENAI_MODEL` | Manual | `gpt-5.6-luna` |
| `OPENAI_MIN_REQUEST_INTERVAL_SECONDS` | Manual | `30` |
| `PUBLIC_BASE_URL` | Manual | The exact HTTPS URL from step 1 |
| `NOTEIQ_DATABASE` | Manual | `/tmp/noteiq.sqlite3` |

Set ingress to **External**, target port to **8000**, and scale to **minimum 1 / maximum 1**.
One running replica is required because the process renews Graph subscriptions in the
background and SQLite has one queue consumer.

## 3. Database and replica count

The production configuration uses `NOTEIQ_DATABASE_URL` and Azure Database for PostgreSQL.
It persists through deployments and permits multiple replicas. Follow the
[PostgreSQL setup and migration](postgresql.md).

The SQLite instructions below apply only if you temporarily run without PostgreSQL.

### Temporary SQLite fallback

Keep one active revision and set minimum and maximum replicas to one. `/tmp/noteiq.sqlite3`
uses the container's local filesystem and avoids SQLite locking failures on an Azure Files SMB
mount. Its data is temporary: a replica replacement, restart or deployment requires signing in
again and recreating subscriptions. This is acceptable for the one-user demo only.

Do not put the SQLite file on Azure Files. SQLite depends on filesystem locking semantics that
are not reliable across every network filesystem. Use a managed database before requiring
persistent data, multiple replicas or production traffic.

If an existing revision fails with `sqlite3.OperationalError: database is locked`, deactivate
it and create a replacement from the same image with a local database:

```sh
az containerapp revision deactivate --name noteiq --resource-group noteiq --revision FAILED-REVISION
az containerapp revision copy --name noteiq --resource-group noteiq --from-revision FAILED-REVISION --revision-suffix demo1 --set-env-vars NOTEIQ_DATABASE=/tmp/noteiq.sqlite3 --min-replicas 1 --max-replicas 1
az containerapp revision show --name noteiq --resource-group noteiq --revision noteiq--demo1 --query "{Active:properties.active,Health:properties.healthState,Replicas:properties.replicas}" --output table
az containerapp ingress traffic set --name noteiq --resource-group noteiq --revision-weight noteiq--demo1=100
```

## 4. Finish Entra and Teams

Open the Container App's Application URL and append `/healthz`. It must return:

```json
{"status":"ok"}
```

In **Entra ID → App registrations → note-iq → Authentication**, add this **Web** redirect URI:

```text
https://noteiq.DOMAIN-FROM-THE-COMMAND/auth/callback
```

On the Mac, create or update `.env.prod` with the same public URL and set `APP_ENV=prod`. It is used only to build the Teams
package locally:

```sh
uv run python -m scripts.package_teams
```

Upload `dist/noteiq-teams.zip` in Teams. The Container App now receives the tab requests,
sign-in callback and Graph notifications directly; no Azure Bot Service or gateway is involved.

Microsoft references: [deploy from source](https://learn.microsoft.com/en-us/azure/container-apps/containerapp-up),
[external ingress](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview),
[secrets](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets), and
[SQLite over network filesystems](https://www.sqlite.org/useovernet.html).
