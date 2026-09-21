# Scaling NoteIQ

NoteIQ runs as one container: a FastAPI web tier and a background worker sharing
a single process and a single event loop. That is the right shape for a pilot,
and it is the thing that limits how many enrolled users the service can carry.
This page describes where the limit comes from, what has been done about it, and
how to split the two halves when one container is no longer enough.

## What actually binds

The worker is a loop over a job queue in PostgreSQL. Two kinds of work reach it:

- **Fetches** — a transcript or insight Graph has already published. Priority 0.
- **Polls** — a `MeetingSync` or `UserSync` asking Graph whether anything exists
  yet. Priority 1, so a fetch never waits behind a minute of polling.

Every 60 seconds the worker enqueues one `UserSync` per enrolled user plus one
`MeetingSync` per meeting still waiting on its Copilot insight. That sweep is the
dominant cost: it scales with the number of users and is paid whether or not
anything is happening.

## Reading the logs

Two lines tell you where you are. Both are at `INFO`.

```text
Sweep queued=41 pending=12 running=4
Job id=8123 type=MeetingSync result=SYNCED duration_ms=812
```

`pending` is the queue depth at the top of each sweep. **A `pending` that trends
upward across a working day is the saturation signal** — it means the sweep is
enqueueing faster than the worker drains, and time-to-summary will grow without
bound until the backlog clears. A `pending` that returns to single digits between
sweeps means there is headroom.

`duration_ms` on each job says where the time goes. Expect `MeetingSync` and
`UserSync` in the hundreds of milliseconds (they are Graph round trips) and
`TranscriptEvent` in the tens of seconds when OpenRouter is configured, because
it includes generating the summary.

## What one container can carry

Roughly 20 users comfortably, and that is an estimate from job cost, not a
measurement of a running tenant — use the two log lines above to replace it with
your own number rather than trusting this paragraph.

The arithmetic: at N users the sweep enqueues about N `UserSync` and up to N
`MeetingSync` per minute, and each is one to two Graph round trips. With
connection reuse and four jobs in flight that is well inside a 60-second budget
at 20 users. The next constraint after the sweep is not the worker at all — it is
the B1ms PostgreSQL instance (one burstable vCPU) and the fact that every store
call is synchronous and blocks the shared event loop.

Three limits sit outside NoteIQ and will be reached on their own schedule:

- **OpenRouter free-tier quota.** The fallback models are `:free`, and every new
  transcript segment re-summarises the whole meeting. Around 4 meetings per user
  per day, 20 users is ~80 summaries a day; the free tier allows 50 without
  credits on the account.
- **Graph throttling**, which is per application per tenant.
- **PostgreSQL storage**, which grows mainly from raw transcript text at roughly
  100–300 KB per hour-long meeting.

## Tuning a single container

| Variable | Default | Effect |
|---|---|---|
| `NOTEIQ_JOB_CONCURRENCY` | `4` | Jobs run at once. Raising it helps when jobs are waiting on Graph or OpenRouter, which is the usual case. It does not help with database-bound work, because store calls are synchronous |
| `NOTEIQ_SUBSCRIPTION_CONCURRENCY` | `15` | Graph subscription create/renew calls in flight at once during a renewal cycle. See below |
| `NOTEIQ_MEETING_RETENTION_DAYS` | unset | Unset keeps saved meetings until the user disconnects, which is the documented product behaviour. Setting it deletes meetings and their transcripts past that age. Minimum 7, since discovery itself looks back 7 days |
| `OPENAI_MIN_REQUEST_INTERVAL_SECONDS` | `30` | Minimum interval between OpenAI requests in each worker process. Leave at 30 seconds (2 RPM) initially; more worker replicas multiply the total ceiling |

Finished jobs are pruned hourly without configuration: ordinary results after a
day, failures after seven so they remain available for diagnosis.

## Subscription renewal

Every enrolled user needs two Graph subscriptions (transcripts, insights) kept
alive with an hourly expiry, renewed on a 15-minute timer. At a few hundred
users this used to mean walking every enrolled user in Python and rescanning
Graph's own subscription listing for each one — an O(users × subscriptions)
cost that, at 1500 users, took the renewal cycle several minutes and stalled
the rest of the worker's housekeeping (notifications, pruning) for as long as
it ran.

A local `subscriptions` table now tracks each one's expiry. Every cycle still
lists Graph's subscriptions once, to clean up orphans and to correct the local
table against drift (a lost row, a fresh deploy, a manual change), but the
decision of *what needs a Graph call this cycle* is one indexed query against
that table instead of a walk over every user. Only the resources that come
back from that query make a network call, bounded by
`NOTEIQ_SUBSCRIPTION_CONCURRENCY` running at once — turning several minutes of
sequential PATCH/POST calls into a few seconds.

## Splitting web and worker

Past one container, the web tier is what needs to scale out, and it cannot while
the worker lives inside it: every replica would run its own 60-second sweep and
its own subscription renewal, duplicating Graph calls rather than sharing them.

`NOTEIQ_ROLE` separates them.

| Value | Runs |
|---|---|
| `all` (default) | Web tier and worker in one process. Must stay at one replica |
| `web` | HTTP only. Safe to scale to several replicas |
| `worker` | Background loop only. **Must stay at one replica** |

Both roles need the same environment, the same secrets and the same
`NOTEIQ_DATABASE_URL`; they coordinate entirely through the database. The worker
container still answers `/healthz`, so the platform can probe it normally.

```sh
# Web tier: scales out.
az containerapp update --name noteiq --resource-group noteiq \
  --set-env-vars NOTEIQ_ROLE=web --min-replicas 1 --max-replicas 5

# Worker: exactly one replica, always.
az containerapp create --name noteiq-worker --resource-group noteiq \
  --environment noteiq-uae --image THE-SAME-IMAGE --min-replicas 1 --max-replicas 1 \
  --ingress internal --target-port 8000 --set-env-vars NOTEIQ_ROLE=worker
```

The worker must stay at a single replica because the sweep and the subscription
renewal are timers, not queue work, and nothing elects a leader between two of
them. The job queue itself is already safe for several consumers — claims use
`FOR UPDATE SKIP LOCKED` with a 300-second lease — so removing the timers is the
only work a multi-replica worker would need.

Requests that ask for a subscription repair (sign-in, Reconnect, Disconnect, and
a Graph lifecycle notification) record the request in the database as well as
signalling in-process, so they still reach a worker in another container.

## What has not been done

- **Store calls are synchronous** and block the event loop for their duration.
  This is deliberate for now: it is also what makes a read-modify-write like
  `save_meeting` atomic against the concurrent jobs added here, since no `await`
  inside it lets the loop switch. Making the store async requires adding explicit
  per-meeting locking at the same time.
- **`GET /api/meetings` returns up to 100 meetings with full content**, including
  every rendered Adaptive Card, and each open tab requests it every 15 seconds.
  Splitting it into a list view and a per-meeting detail fetch is the largest
  remaining win on the web side.
- **PostgreSQL is a single B1ms instance.** Burstable CPU credits are the thing
  to watch once the worker is no longer the constraint.
