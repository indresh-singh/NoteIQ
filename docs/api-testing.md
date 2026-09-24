# Debug NoteIQ with Postman

Import both files from `postman/` into Postman Desktop, then select the **NoteIQ diagnostics**
environment. Run requests individually, in order. These requests retrieve data; they do not
populate NoteIQ or send notifications.

## 1. Test Microsoft directly from your Mac

Set these environment values locally in Postman:

| Variable | Value |
|---|---|
| `tenant_id` | Directory ID for your Microsoft 365 tenant; prefilled, verify it |
| `client_id` | NoteIQ's Graph application ID; prefilled, verify it |
| `client_secret` | Secret **Value** from that same app registration |
| `organizer_id` | Meeting organizer's Entra user Object ID, not the client ID |
| `meeting_join_url` | Full Teams meeting join link, without Markdown or manual encoding |

Keep secrets and tokens local; do not commit or share a populated environment export.

1. Send **01 Get Graph app token**. Expected: HTTP 200. The token is saved automatically.
2. Send **02 Find meeting using join URL**. Expected: nonempty `value`. Verify the subject
   and organizer. The first result is saved as `meeting_id`.
3. Send **03 Get meeting** to confirm the ID and organizer.
4. Send **04 List transcripts**, then **05 Download transcript**. Expected: HTTP 200 with
   `WEBVTT` text. Multiple transcripts may exist; select another `transcript_id` if needed.
5. Send **06 List Copilot insights**, then **07 Get Copilot insight**. Expected: structured
   meeting notes and action items. These requests work independently of transcript retrieval.
6. Send **08 Inspect subscriptions**. Look for both resources for your organizer:
   `/users/{id}/onlineMeetings/getAllTranscripts` and
   `/copilot/users/{id}/onlineMeetings/getAllAiInsights`. Check expiration and that
   `notificationUrl` points to the intended NoteIQ host's `/api/graph/notifications`.

If you already have the Graph `onlineMeeting.id`, set `meeting_id` and skip request 02.
Do not use the numeric Teams join code. IDs are URL-encoded automatically by the collection.
List requests capture the first result only; inspect `@odata.nextLink` for additional pages.

Graph calls require the app's existing admin-consented application permissions:
`OnlineMeetings.Read.All`, `OnlineMeetingTranscript.Read.All`, and
`OnlineMeetingAiInsight.Read.All`. The organizer must be covered by the application access
policy for this client ID; Copilot insight access also requires the organizer's Copilot license.

## 2. Inspect NoteIQ from Postman

`noteiq_base` defaults to the deployed URL. Send **09 Health** first.

Open that same URL in a normal browser and connect Microsoft 365. In browser Developer Tools,
open **Application/Storage → Session Storage → your NoteIQ origin**. Copy `noteiq-session`
into the Postman environment variable `noteiq_token`. Treat it as a password. If session
storage is unavailable, inspect the Authorization header of the browser's `/api/me` request.
Do not paste the token into chat.

Send **10 Signed-in user and status**, then **11 Saved meetings**. From a meeting's
`content.transcripts[].transcript.local_id`, copy the integer into `local_transcript_id` and
send **12 Saved transcript**. Graph bearer tokens cannot authenticate to these NoteIQ endpoints.

`/api/meetings` is exactly what Refresh reads. It does not request old artifacts from Graph.
A Graph result with an empty NoteIQ list points to missed events, retrieval/worker errors,
organizer filtering, or lost local storage. A healthy `/healthz` alone does not rule these out.

## 3. Run the Python backend locally if needed

For direct Graph requests above, no local server, Docker, tunnel or Azure shell is needed.
To inspect the Python app itself, select the existing `.env.dev` and run:

```sh
NOTEIQ_DATABASE=data/local-debug.sqlite3 uv run python -m scripts.serve
```

Set `noteiq_base=http://127.0.0.1:8000` for the local health request. Local storage is separate
from Azure: a deployed session token does not work locally. Full local sign-in and Graph webhook
testing require a public HTTPS tunnel pointing to port 8000, `PUBLIC_BASE_URL` set to that tunnel
origin, and its `/auth/callback` registered as a Web redirect URI in Entra. Use the tunnel URL
for browser sign-in and Postman `noteiq_base`.

Use a distinct tunnel origin for a local worker. Do not run a local worker with the production
`PUBLIC_BASE_URL`: subscription cleanup uses that URL and can interfere with deployed subscriptions.
Postman must not add an unrelated `Origin` header to NoteIQ POST requests.

## Interpret results

| Result | Meaning / next check |
|---|---|
| Token request fails | Tenant, client ID, secret value or secret expiry |
| Graph 403 | Read `error.code`, `error.message`, and `innerError`; check consent, organizer policy and license |
| `SpeakerAttributionNotAllowed` | Retry transcript content with `Accept: application/vnd.microsoft.graph.transcript+text` |
| `GraphAccessToTranscriptsDisabled` | Tenant administrator must enable Graph transcript access |
| Graph 404 | Check organizer and Graph meeting/artifact IDs; artifact may not yet be available |
| List returns empty `value` | No artifact returned for this user/meeting at this time |
| NoteIQ 401 | Obtain a new NoteIQ session from the same server/database |
| Graph has artifacts, NoteIQ is empty | Inspect subscription destination/expiry, active revision, worker logs and database persistence |
| NoteIQ has content, no notification | Check current Teams package/RSC consent and notification delivery logs |

Teams recap visibility is not proof of availability through the insight API; Microsoft documents
up to four hours after the meeting ends. A list result is the direct test.

For your shell error, the screenshot shows `exec` selecting failed revision `noteiq--activity1`.
To inspect the running revision explicitly:

```sh
az containerapp exec --name noteiq --resource-group noteiq --revision noteiq--0000003 --command /bin/sh
```

That selects the older running revision, not the newer code. Inspect routing separately:

```sh
az containerapp ingress traffic show --name noteiq --resource-group noteiq --output table
```

References: [Transcript content](https://learn.microsoft.com/en-us/graph/api/calltranscript-get?view=graph-rest-1.0),
[meeting insights](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/meeting-transcripts/meeting-insights).

## Custom transcript uploads

In NoteIQ, open **Upload transcript**, enter a meeting title, select a UTF-8
TXT, VTT or SRT file and choose **Generate summary and action items**.
Results appear exclusively in the Upload transcript tab, with collapsible Summary
and Action Items sections, responsible people, and the shared ClickUp export.
Only the latest successful custom run is retained per user; a new successful run
replaces previous custom records and transcript text. Failed analysis preserves
the last successful result. The Meetings tab shows only Teams-synced records.

Requires `OPENAI_API_KEY`, or both `OPENROUTER_API_KEY` and `OPENROUTER_MODEL`, in the
running service. The upload feature prefers OpenAI when `OPENAI_API_KEY` is configured.
When both providers are configured and OpenAI cannot complete the analysis (including an
invalid API key, a service error, or a connectivity failure), it retries through OpenRouter.
Otherwise it uses OpenRouter directly.
The transcript is sent to that external provider; the key stays on the server.
Limit: 60,000 characters. Analysis runs during the request (allow up to 90 seconds
in your API client). Failed generation saves no new meeting; retry the upload.

Authenticated API: `POST /api/transcripts/upload`, JSON body:

```json
{
  "subject": "Project planning",
  "filename": "planning.txt",
  "text": "Ada: I will send the proposal tomorrow."
}
```

Use the same NoteIQ bearer session as other protected endpoints.
Returns `{"status":"saved","meeting_id":"upload:..."}`.
Read results through `GET /api/meetings`. Uploaded records belong to the
signed-in user and are excluded from Graph synchronization.
