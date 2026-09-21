# NoteIQ

Microsoft 365 Copilot meeting transcripts, notes and action items in a **Teams personal tab**. Python 3.12, **uv**, FastAPI, Microsoft TeamsJS, Adaptive Cards and PostgreSQL. Runs in Azure Container Apps without Azure Bot Service.

**Install → open → connect Microsoft 365 → see meeting cards → click Summary, Action items or Transcripts.**

The app subscribes to transcripts and insights for connected users, verifies the organizer, and groups the results by meeting. Copilot does the summarization. NoteIQ sends Activity-feed notifications directly to the organizer using the app identity, without Azure Bot Service. See [the upgrade steps](docs/transcripts-and-messages.md).

## Launch

Follow [the short setup guide](docs/setup.md) for the Entra app settings and Teams installation.
For deployment, follow [Azure Container Apps deployment](docs/container-apps.md). The
repository includes a production Dockerfile that runs as a non-root user. Use the
[PostgreSQL setup](docs/postgresql.md) for persistent Azure storage.

```sh
uv sync --locked
uv run python -m scripts.configure
uv run python -m scripts.package_teams
uv run python -m scripts.serve
```

The configuration helper asks for four values and generates the webhook secret. Local serving is useful for development; the deployed Container App supplies the public HTTPS endpoint used by Teams, Entra and Graph.

## Diagnostic logging

The server emits detailed UTC logs by default at `INFO`. Every browser request has an
`X-Request-ID` (also returned in the response), and every background job has a job ID.
External Graph, ClickUp and OpenRouter calls record the operation, status, latency,
response size, provider request IDs, retries, and sanitized provider error details.
Unhandled and internally recovered failures include their full exception chains and
tracebacks.

Set `NOTEIQ_LOG_LEVEL=DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` to change the
threshold. OAuth codes, authorization headers, cookies, API keys, transcript/meeting
content, and URL query values are never logged; sensitive upstream JSON keys are
redacted and Microsoft resource identifiers in request URLs are hashed.

## Included

- Real Microsoft work-account sign-in through a Teams-compatible popup.
- Automatic user enrollment from the authenticated identity, without a pilot-ID environment variable.
- Durable subscriptions, queued processing and stored results, using PostgreSQL in Azure and SQLite locally.
- Summary, Action items and Transcripts buttons; the open tab refreshes every 15 seconds.
- Sign out, or disconnect to stop collection and delete saved NoteIQ cards.
- A Teams ZIP generator and a one-user administrative access-policy script.
- Browser SDK files bundled locally, with pinned versions and licenses.

The Teams SDK Python card models remain available for validation; TeamsJS handles the embedded tab. There is no bot runtime. [Architecture and limitations](docs/architecture.md) describe the authentication and data flow.

## Demo preparation

Sign in before your demo meeting, organize a scheduled Teams meeting and start transcription. Copilot insights may take **up to four hours** to become available, so prepare a meeting in advance. [Microsoft's timing documentation](https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/meeting-transcripts/meeting-insights#limitations)

To recover a meeting whose insights were generated before you connected, see the recovery command in [setup](docs/setup.md#recover-an-existing-meeting).

## Verification

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

An optional offline Chromium check exercises the page, popup handoff, buttons and disconnect:

```sh
uv run --with playwright python -m playwright install chromium
uv run --with playwright python -m scripts.browser_smoke
```

This browser check simulates Microsoft sign-in; it does not validate the live tenant.

The tests mock Microsoft endpoints and cover OAuth handoff, enrollment, private results, notifications, retries and storage. Live tenant sign-in, consent, Graph access and Teams installation still require the setup steps.

`uv.lock` is authoritative. To refresh the bundled browser libraries, run `uv run python -m scripts.vendor_assets` (requires network access).

Teams Activity notifications require installation of the updated Teams package and consent to its `TeamsActivity.Send.User` permission. Notifications come from NoteIQ; there is no chat sending or delegated user-token cache. Disconnect before uninstalling the app; uninstall alone does not stop the background subscription.
