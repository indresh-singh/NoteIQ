# Provider date investigation — September 25, 2026

Investigated `10x project - Daily Brief` using production application logs, the
served JavaScript, and a read-only PostgreSQL transaction inside the container.
No production data or deployment was changed.

## Confirmed cause

Production app `dev-daio-noteiq` was serving revision `dev-daio-noteiq--0000016`,
image `noteiq:release2609232045377ee87446770`. Its `updateProviderText()` selects
the first insight with `endDateTime` from the selected provider, without sorting.
If no insight has that field, it uses `content.transcript.createdDateTime`.

Stored meeting row 30 (meeting hash `499522b3`) has 11 transcripts and 10 Copilot
insights, plus one legacy combined summary from each external provider:

| Value selected | Stored UTC timestamp | UAE time (UTC+4) |
| --- | --- | --- |
| First Copilot insight in stored order | September 23, 12:50:10 | September 23, 16:50:10 |
| Latest Copilot insight, already stored | September 24, 13:09:09 | September 24, 17:09:09 |
| Transcript fallback used for OpenRouter | September 24, 12:10:25.5018284 | September 24, 16:10:25 |

The first and third rows reproduce the screenshots exactly. The September 24
Copilot insight is present; the UI selects an older timestamp while displaying
insights from the recurring meeting umbrella. The external summaries have no
`endDateTime` or session identifier.

## Why link recovery did not change the date

At 09:49:51 UTC, recovery successfully resolved this recurring meeting and queued
one check. At 09:49:53–54 UTC, sync found 11 transcripts and 10 insights, with
`new=0` for both. Another check at 09:53:34–36 UTC returned the same counts.
Recovery discovered no missing artifacts and could not change the UI's date
selection rule.

The recovered meeting's metadata refers to the original September 9 series
start. That timestamp must not be used as the date of every subsequent call.

“Meeting found” acknowledges lookup and queueing, not completed retrieval of new
insights. Production keeps it in the shared `syncMessage`: another Refresh can
overwrite it, and a leftover `syncThrottled` flag can clear it when throttling
ends. Logs do not establish which of those UI paths the reported disappearance
followed.

## Local correction and verification

The existing September 24 session implementation (`a26c6e3`) is newer than the
production image. It separates calls, joins Copilot insights by transcript
correlation ID, and uses one transcript-based session date across providers.
It also refetches historical transcript metadata and rebuilds AI summaries per
session. Legacy combined summaries are not assigned to arbitrary sessions.

This investigation adds a dedicated recovery status beside the form. Polling,
manual Refresh and throttle expiry cannot replace it; a new recovery attempt or
sign-out resets it. Success text explicitly describes a queued background check
and explains that existing results can remain unchanged.

The offline browser regression now checks provider-independent session dates,
session-specific content and persistent recovery success/error messages. Run:

```sh
uv run --with playwright python -m scripts.browser_occurrences_smoke
uv run pytest tests/test_occurrences.py tests/test_meeting_lookup.py tests/test_frontend_source.py tests/test_web.py
```

Production requires deployment of the current session implementation and these
UI changes. After deployment, Refresh/link recovery can backfill historical
session metadata through the worker; browser reload alone cannot upgrade the
server or reconstruct those fields. See [recurring meetings](recurring-meetings.md).
