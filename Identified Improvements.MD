# Identified Improvements

This document records findings for evaluation. Items are **not approved for implementation** unless their verification steps have been completed and a corrective option has been selected.

## IMP-001: Make manual Refresh queue-driven

**Status:** Open — evaluate and verify before corrective action

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

- **Owner:** Unassigned
- **Decision date:** Unset
- **Selected option:** Unset
- **Verification evidence:** Pending
- **Implementation authorization:** Not granted
