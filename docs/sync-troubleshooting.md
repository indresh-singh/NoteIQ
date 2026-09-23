# Meeting updates

NoteIQ uses Microsoft's Copilot insights, without generating a replacement summary.
Summary topics keep their original text and expandable subpoints.

- Graph notifications queue new transcripts and insights automatically.
- Saving a transcript or insight also queues a check for the other artifact.
- Every minute, NoteIQ discovers transcripts for meetings organized by each
  enrolled user in the last seven days using Graph `getAllTranscripts`. This
  recovers meetings that never reached NoteIQ through a webhook. It uses the
  existing `OnlineMeetingTranscript.Read.All` application permission.
- Saved meetings updated in the last seven days are also checked for missing artifacts.
- **Refresh** runs both checks against Graph immediately, inline in the request —
  not queued for the background worker. It does not force a subscription repair;
  use **Retry connection** (`/api/reconnect`) for that. Only a newly discovered
  transcript or insight is queued for content-fetch, the same way a
  webhook-delivered one would be.
  - It reports **"Found new activity"** only for a meeting NoteIQ has not saved
    yet or a new Copilot insight. A new transcript for a meeting already shown
    is fetched but not announced.
  - Four meetings are checked at once per person, and at most eight across
    everyone refreshing on one replica.
  - A Refresh stops after 80 seconds, or as soon as Graph throttles the
    tenant (HTTP 429). The message then says how many meetings were checked,
    and the background sweep checks the rest within minutes. The browser waits
    90 seconds.
  - One Refresh runs per person at a time, across tabs and replicas. A second
    click gets "Already checking Microsoft 365" and shows the first check's
    result when it finishes (`GET /api/sync/status`). A lock left by a crashed
    request expires after two minutes.
  - Look for `Refresh completed ... checked= total= complete= timed_out= throttled=
    duration_ms=` in the logs to see how long Refresh takes in your tenant.
- For meetings older than seven days, use **Check meeting** with the meeting link
  or `scripts.recover`. Only organizer meetings are supported; Graph's discovery
  endpoint does not support channel meetings. See Microsoft's
  [getAllTranscripts documentation](https://learn.microsoft.com/en-us/graph/api/onlinemeeting-getalltranscripts?view=graph-rest-1.0).

## Graph throttling (HTTP 429)

Graph limits requests per tenant. When it replies 429, NoteIQ pauses **all**
of its Graph work for as long as Graph's `Retry-After` header says, or 30 seconds
if the header is missing. The pause is stored in the database, so every replica
honours it.

- **Worker:** stops claiming jobs, and skips the background check, subscription
  renewal and Teams notifications, until the pause ends. Jobs already running
  finish on their own.
- **Throttled jobs and notifications:** they come back exactly when Graph allows
  and do **not** use up one of their five attempts.
- **Subscription renewal:** a throttled renewal leaves every user's status
  unchanged, so no one sees a connection error. Deferred users are retried after
  a minute, and a forced repair is kept until it can run.
- **When throttling lasts too long:** every two minutes, and even during a pause,
  the worker checks stored subscription expiry times. It makes no Graph call for
  this. If a subscription has expired or expires within 10 minutes (about 20
  minutes of failed renewal), the user sees **"Microsoft 365 is limiting
  requests, so new meeting updates are delayed. NoteIQ will reconnect and catch
  up automatically."** (`UPDATES_DELAYED`).
  - It replaces only `LISTENING`/`CONNECTING`, never a more specific status.
  - It clears on the next successful renewal. Anything missed is recovered by
    the discovery check.
  - Log line: `Live meeting updates delayed: subscriptions lapsing`.
- **Banner:** while a pause lasts, the tab shows **"Microsoft 365 is limiting
  requests. New meetings and insights are delayed; NoteIQ will catch up
  automatically."** `/api/me` reports `graph_throttled_seconds`, and the tab
  re-reads when the pause ends so the banner clears on its own.
- **Refresh:** makes no Graph calls during a pause. It says "Microsoft 365 is
  limiting requests right now. Try again in about N seconds." It does not
  promise background checks while paused, because those are paused too.
- **Check meeting and Planner:** while a pause lasts, these answer at once with
  "Microsoft 365 is limiting requests right now. Try again in about N seconds."
  (HTTP 429 with `Retry-After`) instead of calling Graph. A 429 during the call
  gets the same message after waiting at most 5 seconds, instead of a generic
  error. A Planner export stopped part-way says how many tasks were sent;
  trying again sends only the rest. ClickUp does not use Graph and is
  unaffected.
- **Logs:** look for `Graph throttled`, `Graph work paused by throttling`,
  `Graph work resumed after throttling` and `Job throttled by Graph`.

Teams and a browser have separate sign-in sessions. Connect with the organizer's
account in Teams. NoteIQ checks the Teams account against its authenticated session
and asks you to reconnect if they differ. Teams context never grants API access.

Notifications appear in **Teams Activity**, not meeting chat. A Graph `204` means
Microsoft accepted delivery; banners depend on Teams notification settings.


Configure `NOTEIQ_DATABASE_URL` for persistent PostgreSQL storage. Without it,
NoteIQ falls back to SQLite; container-local data can be lost on replacement.
Do not run SQLite directly on the Azure Files share.
