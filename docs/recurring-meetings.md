# Recurring meetings

NoteIQ keeps one meeting umbrella and separates actual Teams sessions using the
transcript's `callId`. `onlineMeeting.meetingType=recurring` identifies a recurring
umbrella. No Calendar API or additional Graph permissions are required.

The latest session opens by default; older sessions are collapsed and sorted by
transcript creation time. Each session has its own provider selection, Summary,
Action items, Transcripts, Regenerate, and ClickUp/Planner export controls. A
single session of a non-recurring meeting retains the ordinary card layout.
Collapsible entries include the meeting name and local date/time, for example
`Daily Brief - 10 Oct 10.15 AM`.

Invite type does not determine call boundaries: people can reuse and join any
meeting link at another time. Transcript parts are combined only when Microsoft
supplies the same `callId`, including when transcription was paused and resumed
during that call. Distinct call IDs identify separate calls. When historical
records have no call ID, each transcript remains separate until Refresh recovers
its metadata; NoteIQ does not guess from `scheduled`, `recurring`, calendar time,
or proximity. Duplicate historical transcript aliases with identical correlation
IDs and creation times are presented and summarized once, without deleting the
stored originals.

All transcript fragments with the same call ID are summarized together. Different
calls never share AI inputs or results. Export deduplication includes the session,
and exported task descriptions include its timestamp. As before, materially
rewritten action text after regeneration can create a new task when exported.

This identifies actual calls, not Outlook calendar occurrences: leaving and later
rejoining after the call has ended can create another session. If Microsoft does
not supply a call ID for a recurring or unknown meeting type, each distinct
transcript stays separate; NoteIQ does not infer a session from the title,
calendar day, or a time gap.

## Existing data

Existing transcripts and summaries remain stored. Older combined AI summaries
of reused meeting links are not assigned to individual sessions because they may
contain multiple calls. When every transcript has the same call ID, the existing
whole-call summary remains available; a newly generated summary supersedes it.
Once every occurrence has a replacement from a provider, its old combined
summary no longer triggers the unassigned-summary warning.
Copilot insights are matched by an unambiguous transcript content correlation ID;
unmatched insights are likewise excluded from session action lists and exports.

Refresh rechecks recent meeting umbrellas, refetches available historical
transcripts missing session metadata once, and generates scoped AI results for
configured providers through the normal worker. Background sync does this too
for meetings still eligible for polling. Older meetings outside the recent sync
window need the existing meeting-link recovery workflow. Once metadata is
available, Regenerate can rebuild an individual session's provider result.
Unavailable or expired Graph artifacts cannot be reconstructed automatically.

No database migration or deletion is required: session metadata lives in the
existing meeting JSON. Concurrent JSON updates are serialized, and AI responses
from an outdated transcript snapshot cannot overwrite newer session results.

ChatGPT Enterprise returns at most ten prioritized action items. The same limit
applies per meeting/occurrence to previously saved summaries, displayed actions,
email drafts and task exports. The UI and email drafts number ChatGPT action
items; OpenRouter and Copilot retain their existing action counts.

## Verification

Run `uv run pytest tests/test_occurrences.py` for ingestion, isolation, historical
metadata recovery, concurrent writes, regeneration, and export regressions.
Run `uv run --with playwright python -m scripts.browser_occurrences_smoke` for the
offline browser check (requires Playwright Chromium). It simulates Microsoft
sign-in and task exports; it makes no live provider or task-service requests.
