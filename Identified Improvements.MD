# Identified Improvements

This document records findings for evaluation. Items are **not approved for implementation** unless their verification steps have been completed and a corrective option has been selected.

## IMP-001: Make manual Refresh queue-driven

**Status:** Implemented (step 1: bounded inline refresh) — 2026-09-23

**Area:** Manual meeting refresh (`POST /api/sync`)

### Observed design

The Refresh endpoint currently performs Microsoft Graph discovery and then re-checks every recent saved meeting before returning a response. The browser API helper normally aborts a request after 20 seconds.

Relevant code:

- `app/sync.py`: `sync_now()` performs discovery, then loops through all recent meeting candidates and calls `sync_meeting()`.
- `app/web.py`: `POST /api/sync` awaits `sync_now()` within the HTTP request.
- `web/app.js`: `api()` uses a 20-second default timeout.

The response's `queued` value is calculated as the difference between queue depth before and after the operation. A concurrently running worker can claim a job during that interval, so this number may not accurately represent newly queued work.

### Risk to evaluate

- Refresh may exceed the browser timeout as the number of recent meetings grows or Graph latency increases.
- A browser timeout can make a successful server-side refresh appear to have failed.
- Sequential Graph calls can make the endpoint slow even when the worker queue is available.
- The reported `queued` count may be zero or negative despite useful work being found.

### Verification required

Before choosing a change, gather evidence from a representative environment:

1. Measure `/api/sync` duration at 1, 5, 20, and 100 recent meetings.
2. Record Graph call count and latency for discovery and per-meeting sync.
3. Confirm whether users experience the 20-second browser timeout.
4. Run a refresh while a worker is actively claiming jobs; verify whether queue-depth-difference reporting is inaccurate.
5. Confirm the desired UX: immediate acknowledgement versus waiting for Graph results in the same request.
6. Confirm Graph throttling limits and an acceptable concurrency level for the tenant.

### Corrective options — do not implement until selected

| Option | Description | Advantages | Trade-offs |
| --- | --- | --- | --- |
| A. Queue-driven refresh | Enqueue a per-user discovery/sync job and return immediately with an operation ID and explicit enqueue count. The existing UI polling displays saved results. | Predictable HTTP latency; uses the durable queue; retries survive restarts. | Results are asynchronous; UI needs clear progress messaging. |
| B. Bounded inline refresh | Continue checking Graph in the request, but cap concurrency and stop at a short server deadline. Queue remaining work. | Can show some immediate results. | More complex behavior; partial-result semantics need design and testing. |
| C. Keep current behavior | Raise the client timeout and retain sequential inline checks. | Lowest initial change. | Does not address scaling, request cancellation, or inaccurate queue counts. |

### Preferred option for evaluation

Option A is the preferred direction because the application already has a persistent job queue and the browser already polls for meeting updates. This is a recommendation, not an implementation decision.

Suggested response shape if Option A is approved:

```json
{
  "queued": 1,
  "operation_id": "sync-<opaque-id>",
  "status": "accepted"
}
```

`queued` should be returned from the enqueue operation itself, rather than calculated from total queue depth before and after the request.

### Acceptance criteria if approved

- The Refresh HTTP response completes within an agreed target (for example, under two seconds) independent of the number of recent meetings.
- Refresh work is durable and retried through the existing job mechanism.
- The API reports explicit, non-negative enqueue/discovery counts.
- The UI communicates that checking is in progress and continues to show results as they arrive.
- Tests cover concurrent worker claims, Graph delays/failures, duplicate refresh requests, and restart recovery.
- Production telemetry records refresh duration, queued work, completion/failure state, and Graph throttling without logging meeting content.

### Decision record

- **Owner:** Indresh Singh
- **Decision date:** 2026-09-23
- **Selected option:** B. Bounded inline refresh. Option A was set aside because
  Refresh was queue-driven before commit d2ef4b0 and clicks felt useless: queued
  checks waited behind the background sweep. Keeping the check inline keeps
  results arriving in the same request.
- **Confirmed requirements:**
  - "Found new activity" is reported only for a new meeting or a new AI
    insight, never for a new transcript on a meeting already shown.
  - Browser timeouts are increased.
  - The app must not go back to a Refresh that only queues work.
- **Verification evidence:** Local synthetic run, mocked Graph at 250 ms per
  call. These are not tenant measurements.

  | Recent meetings | Graph calls | Before | After |
  | --- | --- | --- | --- |
  | 1 | 4 | 1.0 s | 0.8 s |
  | 5 | 16 | 4.0 s | 1.5 s |
  | 20 | 61 | 15.4 s | 4.1 s |
  | 100 | 301 | 76.0 s (browser timed out at 20 s) | 19.2 s |
  | 300 | 901 | — | 57.2 s |

  With a worker claiming jobs concurrently, the old queue-depth count reported
  `queued=1` for 20 queued jobs.
- **Implementation authorization:** Granted 2026-09-23.

### What was implemented

- **Correct counts.** `sync_now()` counts `new_meetings` (discovered transcripts
  whose meeting is not saved) and `new_insights` where they are found, instead
  of taking the difference in queue depth.
- **Parallel checks with limits.** Four meetings are checked at once per
  Refresh (`REFRESH_LANES`), with at most eight concurrent Refresh Graph calls
  per replica (`REFRESH_GRAPH_SLOTS`).
- **Deadline.** A Refresh stops after 80 seconds (`REFRESH_DEADLINE_SECONDS`),
  newest meetings first, and returns `checked`/`total`/`complete`.
- **Throttling.** Refresh stops at the first Graph 429 (`throttled: true`); the
  background sweep continues.
- **One Refresh per person.** The first click takes a lock
  (`Store.claim`/`release`, `temporary` table, 120 s expiry). A second click
  gets `{"status": "already_running"}`. The last outcome is kept 10 minutes for
  `GET /api/sync/status`.
- **Browser timeouts.** Refresh now waits 90 s, Check meeting 60 s, and all
  other requests 30 s (was 20 s).

### Known limitations and next steps

- **Repeated announcements.** A meeting or insight whose content fetch keeps
  failing is announced as new on every Refresh until it is saved.
- **Very large mailboxes.** Above roughly 300 recent meetings at 250 ms Graph
  latency, Refresh reaches the deadline and relies on the sweep for the rest.
  Measure `Refresh completed ... duration_ms` in production; if it approaches
  80 s, revisit Option A with a Refresh priority tier.
- **Per-replica cap.** `REFRESH_GRAPH_SLOTS` applies to each replica, not the
  tenant. Recheck it against the tenant's Graph limits when adding replicas.

## IMP-002: Honour Graph throttling (HTTP 429) tenant-wide

**Status:** Implemented — 2026-09-23. Authorized by Indresh Singh.

### Problems fixed

1. **The queue ignored `Retry-After`.** Throttled jobs came back after the fixed
   60 s, 120 s, 240 s, 480 s backoff, were throttled again, and used up their
   five attempts.
2. **There was no tenant-wide pause.** A 429 delayed only the job that got it;
   the worker, the background check, renewal and notifications kept calling
   Graph.
3. **Renewal throttling looked like an outage.** A 429 during subscription
   renewal set every user to `CONNECTION_ERROR`.

### What was implemented

- **Pause hook.** `GraphClient` reports every 429 through its `on_throttle`
  hook, using `Retry-After` or 30 s by default. `Store.pause_graph` records one
  tenant-wide pause in the `temporary` table; it can be extended but never
  shortened.
- **Worker.** While the pause lasts, the worker claims no jobs and runs no
  background check, renewal or notifications.
- **Throttled jobs.** They are rescheduled for the end of the pause without
  counting an attempt. `graph_throttle_seconds` finds the 429 behind the
  `RuntimeError(...) from error` wrappers used by the jobs, and ignores AI
  provider 429s.
- **Notifications.** The Teams notification outbox gets the same treatment.
- **Renewal.** A throttled renewal leaves user statuses unchanged, defers the
  remaining users, retries in 60 s, and keeps a forced repair for later.
- **Refresh.** No Graph calls while paused; the response includes `retry_after`
  for the "try again in N seconds" message.

- **Sustained throttling is shown.** Hiding renewal throttling could have hidden
  expired subscriptions. The worker now checks stored `subscriptions.expires_at`
  every 2 minutes (`DELAY_CHECK_SECONDS`), including during a pause. It marks
  users `UPDATES_DELAYED` when a subscription is expired or within 10 minutes of
  expiry (`SUBSCRIPTION_RISK_SECONDS`). A successful renewal clears it; a saved
  insight does not.

- **Honest banner.** `/api/me` returns `graph_throttled_seconds`. The tab shows
  "Microsoft 365 is limiting requests. New meetings and insights are delayed;
  NoteIQ will catch up automatically." and re-reads when the pause ends.
- **Refresh wording.** While paused, Refresh says "Microsoft 365 is limiting
  requests right now. Try again in about N seconds." and no longer claims
  background checks.

- **Clear messages for user actions.** The Check meeting and Planner endpoints
  (`@graph_action` in `app/web.py`) answer at once during a pause, and cap waits
  on a 429 at 5 s (`interactive_requests`). Instead of a generic error they
  return HTTP 429 "Microsoft 365 is limiting requests right now. Try again in
  about N seconds." A partly sent Planner export reports how many tasks were
  sent.

### Remaining

- **Refresh can still wait inside a call.** A `Retry-After` of up to 60 s is
  still waited out inside a Refresh call, capped by the 80 s deadline.
