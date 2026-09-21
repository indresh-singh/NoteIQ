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
  not queued for the background worker — and requests subscription repair. Only
  a newly discovered transcript or insight is queued for content-fetch, the same
  way a webhook-delivered one would be.
- For meetings older than seven days, use **Check meeting** with the meeting link
  or `scripts.recover`. Only organizer meetings are supported; Graph's discovery
  endpoint does not support channel meetings. See Microsoft's
  [getAllTranscripts documentation](https://learn.microsoft.com/en-us/graph/api/onlinemeeting-getalltranscripts?view=graph-rest-1.0).

Teams and a browser have separate sign-in sessions. Connect with the organizer's
account in Teams. NoteIQ checks the Teams account against its authenticated session
and asks you to reconnect if they differ. Teams context never grants API access.

Notifications appear in **Teams Activity**, not meeting chat. A Graph `204` means
Microsoft accepted delivery; banners depend on Teams notification settings.

The September 13 investigation found an active transcript and insight subscription
for Praveen, but Microsoft's insight subscription endpoint rejected Indresh with
“does not have a valid Copilot license.” Fix that user's license before retrying
the connection. This error does not prevent other users receiving content.

Configure `NOTEIQ_DATABASE_URL` for persistent PostgreSQL storage. Without it,
NoteIQ falls back to SQLite; container-local data can be lost on replacement.
Do not run SQLite directly on the Azure Files share.
