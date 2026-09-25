# Microsoft Planner integration

NoteIQ supports two access modes:

- **Group plans without a personal connection:** existing application credentials
  discover plans in the signed-in user's Microsoft 365 groups.
- **Connected personal Planner:** delegated Microsoft access lists the signed-in
  user's plans using `GET /me/planner/plans`. Plan lookup, task previews, duplicate
  checks, task creation and description updates all use that same user's token.
  Expired or revoked delegated access never silently falls back to application access.

## Enable personal Planner

1. In the existing Entra app registration, add Microsoft Graph **Delegated**
   `Tasks.ReadWrite`. Keep the existing delegated `User.Read` permission.
2. Grant administrator consent if required by your tenant's consent policy.
3. Deploy the updated app. The existing Web redirect URI
   `PUBLIC_BASE_URL/auth/callback` is reused; no new redirect is required.
4. In NoteIQ's Account settings, select **Connect personal Planner** and sign in
   with the same work account already connected to NoteIQ. Accept Planner access.
5. Select your plan from the refreshed picker and click **Add Plan**. The first
   saved plan becomes the default export destination.
6. Open a meeting and select **Send action items to Planner**. Use **Refresh tasks**
   in settings to inspect the default plan.

A personal plan belonging to a work account is distinct from a consumer Microsoft
account. This integration uses the configured organizational tenant.

## API compatibility and rollout verification

`PLANNER_GRAPH_VERSION` defaults to `v1.0`. The user plan-list API requires
**delegated** access; its application permission mode is unsupported.

Microsoft documents personal `user` containers under Graph beta. If a known personal
Basic plan is absent from v1.0 results, an administrator can explicitly set
`PLANNER_GRAPH_VERSION=beta` and restart the app to evaluate it. This setting affects
only delegated Planner requests, including task writes. There is no automatic beta
fallback. Microsoft does not support beta APIs for production use.

Before enabling beta for a production deployment, verify in your tenant:

- The personal plan appears after consent and Refresh, and unrelated private plans do not.
- Add Plan, task preview, export and duplicate detection work with that plan.
- Reconnect works after revocation; denial of consent leaves the previous connection intact.
- Test both a personal plan and a shared group plan with the intended users.

A successful empty response with no saved plans shows **No Planner plans found**
with guidance to create a plan in Microsoft Planner and click **Refresh**. Discovery
errors are shown separately. When plans are already saved but there are no more to
add, the picker shows **No additional Planner plans found**. Plan titles returned
by Microsoft are not hidden just because they resemble placeholders;
NoteIQ does not claim that every plan visible in the Planner app is API-accessible.
Premium plan support is not promised by this integration. Tests simulate Graph;
live personal-plan compatibility still requires the user's interactive consent.

## Missing group plans: investigation (discovery behavior unchanged)

Connecting personal Planner selects the delegated `/me/planner/plans` branch and
does not also enumerate group plans. Microsoft describes this endpoint as plans
shared with the user, whereas `/groups/{id}/planner/plans` lists plans owned by
that group. Therefore the current personal list is not a complete group inventory.
The app-only branch enumerates direct Microsoft 365 (`Unified`) group memberships;
failed individual group lookups are logged and skipped. Also, the picker caches
discovery until its own **Refresh** button is used or the connection changes.

To confirm a specific missing plan, use the same account, click Planner **Refresh**,
check the plan's group membership and Basic/Premium type, and compare the user-list
response with the owning group's response and server discovery logs. No tenant
responses were inspected for this investigation. A future fix could explicitly
combine user and authorized group discovery with deduplication and permission
checks; that behavior has not been implemented.

Microsoft references: [user plans](https://learn.microsoft.com/en-us/graph/api/planneruser-list-plans?view=graph-rest-1.0),
[group plans](https://learn.microsoft.com/en-us/graph/api/plannergroup-list-plans?view=graph-rest-1.0).

## Credentials and lifecycle

MSAL handles authorization-code flow, PKCE, nonce validation and silent token refresh.
Consent is bound to the current NoteIQ account and tenant. Microsoft tokens never
leave the server. The browser receives only NoteIQ's existing session token.

Each user's serialized MSAL cache is encrypted with Fernet before database storage,
including the short-lived sign-in handoff. Its key is derived using HKDF-SHA256 from
`GRAPH_CLIENT_SECRET`, separated by purpose, tenant, app and user. Keep that secret
strong and identical across replicas. **Rotating it requires users to reconnect
personal Planner.** No additional encryption environment variable is required.

Both SQLite and PostgreSQL create `planner_connections` automatically. Cache refresh
uses a conditional update so a concurrent refresh cannot overwrite a reconnect or
restore credentials deleted by disconnect.

**Disconnect personal Planner** deletes its credentials and clears saved plan
selections/defaults. It keeps export tracking to prevent duplicates if plans are
added again. It does not delete tasks in Microsoft Planner or revoke the Entra
consent grant. Full NoteIQ disconnect deletes credentials and all local Planner data.

Exports create one task per action, followed by a description update using an ETag.
Owner names are description text, not Graph assignments; due dates and buckets are
not populated. A failed description update is logged while retaining the created task.

## Application mode permissions

Users who have not connected personal Planner retain group-based discovery using
application `GroupMember.Read.All` and `Tasks.ReadWrite.All`, with admin consent.
Group lookup failures may produce partial results in this legacy mode.

## Debugging

The shared HTTP/Graph logger records UTC millisecond timestamps, NoteIQ request IDs,
Graph request IDs, duration, status and sanitized error diagnostics. Delegated
Planner discovery logs its mode and returned plan count. For a missing plan, correlate
`/api/planner/available-plans` with `/me/planner/plans` and check the configured API
version. For 403, inspect the Graph error code: access restrictions and service limits
can both cause 403. Use **Reconnect personal Planner** for expired/revoked consent.

References:
- [List user plans](https://learn.microsoft.com/en-us/graph/api/planneruser-list-plans?view=graph-rest-1.0)
- [Personal plan containers (beta)](https://learn.microsoft.com/en-us/graph/api/resources/planner-overview?view=graph-rest-beta)
- [MSAL cache serialization](https://learn.microsoft.com/en-us/entra/msal/python/advanced/msal-python-token-cache-serialization)
