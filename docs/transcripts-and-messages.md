# Transcripts and Teams Activity notifications

NoteIQ notifies **you in Teams Activity**, using the NoteIQ app identity and app-only
Microsoft Graph access. There is no Azure Bot Service, chat destination or sending as you.
Clicking a notification opens the NoteIQ Meetings tab.

## Activate this version

1. In Entra, retain the existing application permissions `OnlineMeetings.Read.All` and
   `OnlineMeetingAiInsight.Read.All`, plus delegated `User.Read` for sign-in. Add application
   **`OnlineMeetingTranscript.Read.All`** and grant admin consent if not already done.
   The organizer's application access policy must include the current `GRAPH_CLIENT_ID`.
   You can remove the previously suggested delegated `Chat.ReadBasic` and `ChatMessage.Send`
   permissions from NoteIQ; this version does not request or use them.

2. Build and deploy the new Docker image using the [Container Apps guide](container-apps.md).
   Keep one replica and stop the previous revision before starting the replacement.
   The database gains an activity outbox. Old delegated chat caches and old chat outbox
   records are removed automatically; meeting content and transcripts are preserved.
   This version does not fix SQLite network-volume locking or support scale-out.

3. Generate the updated Teams package:

   ```sh
   uv run python -m scripts.package_teams
   ```

   Upload/install **`dist/noteiq-teams.zip`**, version **1.3.0**, in Teams. Accept its permission
   to send you activity notifications. This is `TeamsActivity.Send.User`, declared in the
   package through resource-specific consent (RSC). You do not add that RSC permission
   through the Entra Graph permission picker. Tenant app/RSC policies must allow installation
   and consent; ask your Teams administrator if the updated permission cannot be accepted.
   Opening NoteIQ only in a browser is insufficient: the app must be installed personally
   for the recipient in Teams. Installing the old ZIP does not grant this permission.

4. Open NoteIQ and connect Microsoft 365. Before starting transcription in your next meeting,
   allow the worker to create both subscriptions. If a notification previously failed, update
   the Teams installation and click **Retry notifications** in NoteIQ.

No new Azure resource, redirect URI or delegated chat consent is needed. The package's
`webApplicationInfo.id` uses `GRAPH_CLIENT_ID`; its `resource` uses the public app origin.
The notification deep link uses `TEAMS_APP_ID`, falling back to `GRAPH_CLIENT_ID`, just as
package generation does. If using `--app-id` when packaging, set **the same ID** in the
server's `TEAMS_APP_ID` so notifications open the correct installed app.

## Notifications and meeting organization

A meeting produces exactly one notification:

- **Insights available:** “Your meeting summary and action items are ready. Open NoteIQ
  to review them.” This is queued only after real notes/actions have been retrieved and
  saved, whether Copilot or OpenRouter produced them. Saving a transcript notifies nobody,
  and a transcript event never triggers a fake insights-ready notification.

The outbox key is the meeting, not the artifact, so later insight segments for the same
meeting — the second provider, a regenerated summary, a resumed transcription — update the
stored results silently instead of notifying again. Transcript-ready notifications were
retired; any left queued by an earlier build are cancelled unsent.

The app name/icon represents NoteIQ in the Activity feed. Notification previews contain
readiness text and the meeting topic, not raw transcript or insight bodies. Banner, sound
and mobile presentation depend on Teams settings and client support; validate those in the
demo tenant. The reliable intended surface is the Teams Activity feed.

Each organizer sees one meeting entry with **Summary | Action items | Transcripts**.
The transcript loads only when expanded. Graph meeting IDs group the content, and artifact
IDs suppress duplicates. Multiple transcript/insight segments are retained with timestamps;
recurring meetings that reuse the same Graph meeting ID share an entry. Historical duplicate
rows are retained rather than deleted. Only the signed-in organizer can retrieve the content.

## Processing and retry behavior

A single worker renews transcript and insight subscriptions every 15 minutes. The webhook
validates client state, tenant when supplied and enrollment, then commits the event before
HTTP 202. The worker verifies the organizer, retrieves the artifact and saves it before
queueing the corresponding Activity notification. The same worker delivers the outbox using:

```text
POST /v1.0/users/{organizerId}/teamwork/sendActivityNotification
```

The manifest declares the matching `insightsReady` activity type.
Text topics deep-link to the personal Meetings tab. A stable `chainId` is derived from the
user/event key so retries update the same activity instead of creating a new feed entry.
Transcript and insight chain IDs differ. The outbox retries up to five attempts; failed
notifications remain available for **Retry notifications**, scoped to the signed-in user.
Notification failure does not remove or block saved content.

Transcript content is WebVTT. The documented `SpeakerAttributionNotAllowed` response triggers
fallback to Microsoft's format without speaker names; general access errors are not bypassed.
There is no live audio capture and no API call to initiate Copilot generation. Insights can
take up to four hours to become available. Use a scheduled, transcribed meeting for the demo.

## Storage and disconnect

SQLite holds enrolled identities, temporary OAuth handoff data, session hashes, transcript
text, insights/cards and the activity outbox. No persistent per-user Microsoft token cache
is needed for these notifications. Only NoteIQ's app credentials access the Activity API.
The host operator can access the database; secure storage and backups accordingly.

Signing out ends the browser session; background collection and notifications continue.
Disconnect deletes the user's saved content, transcripts and queued activity notifications,
and requests subscription removal. Previously delivered Activity entries are not deleted.
Removing the Teams tab alone does not signal the backend to disconnect; use Disconnect first.
Microsoft 365 originals remain unchanged.

Avoiding Azure Bot Service does not establish end-to-end UAE residency for Teams, Graph,
Entra or Copilot; those Microsoft services retain their own service commitments.

## Recover a prepared meeting

Run against the deployed database, not a separate local database:

```sh
python -m scripts.recover --user-id ORGANIZER_OBJECT_ID --meeting-url 'FULL_TEAMS_JOIN_URL' --include-transcripts
```

If you already have the Graph online meeting ID, run the recovery command inside the active
Container Apps replica so it uses the same database and worker as NoteIQ:

```sh
az containerapp exec --name noteiq --resource-group noteiq --command "python -m scripts.recover --user-id ORGANIZER_OBJECT_ID --meeting-id 'GRAPH_MEETING_ID' --include-transcripts"
```

Recovery queues existing artifacts and corresponding Activity notifications. It does not
cause Microsoft to generate missing insights. Repeated artifact events reuse their outbox keys.

## Verification

Automated tests cover transcript fetching, organizer isolation, both event types, app-only
Activity HTTP requests, stable retry chain IDs, manifest declarations and retirement of the
old chat credentials. The browser smoke test simulates Microsoft sign-in and exercises cards,
transcript viewing, reload, mobile layout and disconnect. No real tenant notification is sent
by these tests. A live test must confirm app installation/RSC consent and Activity delivery.

Microsoft references:
[Activity API](https://learn.microsoft.com/en-us/graph/api/userteamwork-sendactivitynotification?view=graph-rest-1.0),
[manifest and RSC requirements](https://learn.microsoft.com/en-us/graph/teams-send-activityfeednotifications),
[transcript notifications](https://learn.microsoft.com/en-us/graph/teams-changenotifications-callrecording-and-calltranscript),
[transcript content](https://learn.microsoft.com/en-us/graph/api/calltranscript-get?view=graph-rest-1.0),
[Copilot availability](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/meeting-transcripts/meeting-insights).
