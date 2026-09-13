# Meeting updates

NoteIQ uses Microsoft's Copilot insights, without generating a replacement summary.
Summary topics keep their original text and expandable subpoints.

- Graph notifications queue new transcripts and insights automatically.
- Saving an insight also queues a check for its meeting's missing transcript.
- Every five minutes, NoteIQ checks the ten most recently updated meetings from
  the last 24 hours for missing transcripts and insights.
- **Refresh** queues that same check for the signed-in user. It runs in the
  background; the open page reloads results every 15 seconds and on returning to
  the tab. It does not trigger Copilot generation.
- These fallback checks cover meetings already known to NoteIQ. If neither
  notification arrived, use `scripts.recover` with the meeting link. Subscription
  setup must succeed before the meeting starts for dependable transcript events.

Teams and a browser have separate sign-in sessions. Connect with the organizer's
account in Teams. NoteIQ checks the Teams account against its authenticated session
and asks you to reconnect if they differ. Teams context never grants API access.

Notifications appear in **Teams Activity**, not meeting chat. A Graph `204` means
Microsoft accepted delivery; banners depend on Teams notification settings.

The September 13 investigation found an active transcript and insight subscription
for Praveen, but Microsoft's insight subscription endpoint rejected Indresh with
“does not have a valid Copilot license.” Fix that user's license before retrying
the connection. This error does not prevent other users receiving content.

The demo still uses temporary container-local SQLite. A replacement replica loses
its database unless restored. Use a managed database before relying on durable
enrollment or scaling; do not run SQLite directly on the Azure Files share.
