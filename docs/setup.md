# Set up NoteIQ

Use your existing **note-iq** app registration in the same tenant as Microsoft 365. The **note-iq-bot** registration is unused. No Azure Bot Service is needed.

Deploy the service with the [Container Apps guide](container-apps.md), then return here for Entra permissions, access policy and Teams upload.

## 1. Update note-iq in Azure Portal / Entra

Open **Microsoft Entra ID → App registrations → note-iq**.

1. **Overview:** copy **Application (client) ID** and **Directory (tenant) ID**. Ensure Supported account types is **Accounts in this organizational directory only**.
2. **Certificates & secrets → Client secrets → New client secret:** create one and copy its **Value**, not its Secret ID. A valid existing secret also works. Keep it on the server.
3. **Authentication → Add a platform → Web:** add `https://YOUR-PUBLIC-HOST/auth/callback` as the redirect URI and save. Replace the host with step 1's hostname. This is a **Web** platform, not SPA. Leave implicit access-token/ID-token checkboxes off and public-client flows disabled. No Bot Framework redirect URI or OAuth connection is used.
4. **API permissions → Add a permission → Microsoft Graph:** ensure the following permissions are present:

| Type | Permission | Used for |
|---|---|---|
| Delegated | `User.Read` | Microsoft work-account sign-in |
| Application | `OnlineMeetingAiInsight.Read.All` | Copilot insights and notifications |
| Application | `OnlineMeetings.Read.All` | Meeting title and organizer check |
| Application | `OnlineMeetingTranscript.Read.All` | Transcript notifications and content |
| Application | `Tasks.ReadWrite.All` | Microsoft Planner integration — optional, see [docs/planner.md](planner.md) |
| Application | `GroupMember.Read.All` | Scoping Planner plan discovery to the signed-in user's own groups — optional, see [docs/planner.md](planner.md) |

Click **Grant admin consent** and confirm all required entries show consent granted. Transcript collection requires the application transcript permission above. For Teams Activity notifications, install the updated package using [these setup steps](transcripts-and-messages.md). You do not need to configure **Expose an API** for this popup-based version.

The registered redirect URI must exactly match `PUBLIC_BASE_URL` plus `/auth/callback`. If the public hostname changes, update Entra, `.env` and the generated Teams package together. Expired Graph subscriptions on an old hostname may remain until their one-hour expiry.

Microsoft references: [register a web redirect](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app), [tab popup authentication](https://learn.microsoft.com/en-us/microsoftteams/platform/tabs/how-to/authentication/auth-tab-aad), [meeting insights permissions](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/meeting-insights/callaiinsight-get).

## 2. Enable meeting access for yourself once

In **Microsoft 365 admin center → Users → Active users → your organizer account → Licenses and apps**, verify Microsoft 365 Copilot and Teams are assigned to this user. A subscription being available to the tenant is not the same as a license assigned to the organizer.

The Graph app still needs an **application access policy**. If your existing NoteIQ policy already covers this app ID and user, keep it.

Otherwise, open Azure Portal **Cloud Shell → PowerShell**. Copy and paste the contents of [scripts/grant_access.ps1](../scripts/grant_access.ps1) into it. Alternatively, upload that file using Cloud Shell's upload button and run `./grant_access.ps1`.

The script asks for the tenant ID, the **note-iq** client ID, and your meeting organizer's work email. Sign in as that tenant's Microsoft 365 administrator when prompted. It resolves the user ID automatically, creates or updates **NoteIQ-Pilot**, and assigns it to this one user. It stops if the user already has a different explicit policy rather than overwriting it.

Allow up to **30 minutes** for the policy to propagate. If `Get-CsTenant` fails, the script stops before making policy changes: verify the tenant selected at sign-in, your Teams administrative role and that Teams is provisioned. That error alone does not prove a particular cause. [Microsoft policy instructions](https://learn.microsoft.com/en-us/graph/cloud-communication-online-meeting-application-access-policy)

## 3. Configure and package

### Optional: OpenAI / ChatGPT Enterprise meeting summaries

If your ChatGPT Enterprise organization provides an OpenAI API project key, add these
Container App environment variables as secret references where appropriate:

For local development, put `OPENAI_API_KEY` in the ignored `.env` file at the repository
root; `app.config.settings()` loads it automatically. Never commit that file.

| Name | Value |
|---|---|
| `OPENAI_API_KEY` | Your Enterprise project's API key (secret reference) |
| `OPENAI_MODEL` | `gpt-5.6-luna` (the default) |
| `OPENAI_MIN_REQUEST_INTERVAL_SECONDS` | `30` (the default; 2 requests/minute per worker) |

When `OPENAI_API_KEY` is present, OpenAI is the transcript-summary service and takes
precedence over an optional OpenRouter configuration. The app sends meeting transcripts to
the OpenAI Responses API. Requests use low reasoning effort and medium verbosity, are stored
in the Enterprise project, and cap each request at
20,000 transcript characters and 1,200 output tokens, permits only one in-flight request
per worker process, and spaces requests by the configured interval. Start with one worker
replica; increasing worker replicas multiplies this application-side request ceiling.

In the NoteIQ project folder on your Mac:

```sh
uv sync --locked
uv run python -m scripts.configure
```

Enter the tenant ID, note-iq client ID, client secret Value and public HTTPS origin when prompted. The helper writes a private `.env` and generates the webhook secret. If `.env` exists, edit it instead; the helper preserves existing credentials.

Build the Teams package:

```sh
uv run python -m scripts.package_teams
```

Open `https://YOUR-PUBLIC-HOST/healthz`; it must return `{"status":"ok"}`. Then open the base URL and confirm that NoteIQ loads.

The local default is `data/noteiq.sqlite3`. Azure deployments should set
`NOTEIQ_DATABASE_URL` and follow the [PostgreSQL setup](postgresql.md). PostgreSQL preserves
meeting content across revisions and safely supports multiple replicas.

## 4. Upload to Teams and connect

1. Sign in to **Teams desktop or web** with the same Microsoft 365 work account.
2. Open **Apps → Manage your apps → Upload an app → Upload a custom app**.
3. Select `dist/noteiq-teams.zip` from this project and choose **Add**.
4. Open **NoteIQ → Meetings → Connect Microsoft 365**.
5. Complete Microsoft's account selection/sign-in popup. The app captures your user ID and starts the subscription automatically.

If **Upload a custom app** is missing, the Teams administrator must enable custom-app uploading for your account in the app setup policy and permit custom apps in the organization. See [Microsoft's upload instructions](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/deploy-and-publish/apps-upload).

If you previously installed the bot package under a different Teams app ID, remove that old package or pass its package ID to `scripts.package_teams --app-id YOUR-OLD-TEAMS-PACKAGE-ID` to produce an upgrade. The generated package contains a personal tab and no bot entry.

## 5. Run the demo

Organize a scheduled Teams meeting after connecting. Start transcription, discuss a few decisions and named action items, and end the meeting. Copilot prepares the insights; NoteIQ retrieves them when Graph reports that they are available. The open tab refreshes every 15 seconds.

Microsoft says insight availability may take **up to four hours** after the call. Prepare a real meeting before your presentation. Channel meetings are not supported by this API. [Microsoft limitations](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/meeting-transcripts/meeting-insights#limitations)

**Listening** means the subscription is active, not that every Graph access check has passed. If meeting access fails, NoteIQ shows an access message; check the organizer's Copilot license, both application permissions, consent and the access policy. Click **Retry connection** after correcting them. Recover the affected meeting as below if the original job already failed permanently.

## Recover an existing meeting

After connecting in NoteIQ, run:

```sh
uv run python -m scripts.subscribe
```

It prints connected user IDs and their subscription status. Then use your organizer ID and the **full Teams meeting join link**, enclosed in single quotes:

```sh
uv run python -m scripts.recover --user-id YOUR-ORGANIZER-ID --meeting-url 'FULL-TEAMS-JOIN-URL' --include-transcripts
```

Run recovery inside the deployed container or against the same database. It fetches available transcripts and insight IDs and queues them; it does not fabricate content or cause Copilot to create missing insights.

## Stop collection

Use **Account settings → Disconnect and delete my NoteIQ cards** before uninstalling. Signing out only ends the current app session. Removing the Teams package by itself does not notify this tab-only backend to stop collecting.

Results appear in the NoteIQ Meetings tab. Readiness notifications appear in Teams Activity under the NoteIQ app identity; install the updated package and consent to `TeamsActivity.Send.User`. No chat sending is used. Microsoft 365/Entra/Graph processing remains subject to your tenant's service arrangements; removing Azure Bot Service does not itself establish end-to-end UAE residency.
