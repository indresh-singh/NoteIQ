# Microsoft Planner action-item export

NoteIQ can send Copilot action items to Microsoft Planner, the same way it
does to ClickUp (see [docs/clickup.md](clickup.md)) — with one structural
difference worth understanding before you turn it on: **Planner has no
separate OAuth app or connect step.** It is reached with the same app-only
Graph credentials already used for transcripts, insights and Activity
notifications, because the identity is already the one that signed into
NoteIQ via Microsoft SSO. There is no ClickUp-style "Connect" button, no
client secret to generate, no per-user token stored, and no feature flag —
it's on unconditionally, the same as transcript and insight processing. Only
two extra Graph permissions on the app registration NoteIQ already uses are
needed before it actually works.

## What it does

- **Write:** clicking **Send action items to Planner** on a meeting creates
  one Planner task per action item, in the user's chosen plan. A second
  click skips tasks NoteIQ already created for that same action item (and
  re-creates one only if it was deleted from Planner itself).
- **Retrieve / show what exists:** Account settings → Microsoft Planner has
  an **Existing tasks in the default plan** section that lists what is
  already in the connected plan, independent of anything NoteIQ exported —
  so a person can see the plan's current state, not just what they just sent.

## One-time Azure/Entra setup

1. Open **Microsoft Entra ID → App registrations → note-iq → API permissions
   → Add a permission → Microsoft Graph → Application permissions** and add:

   | Permission | Why |
   |---|---|
   | `Tasks.ReadWrite.All` | Create, read and update tasks in Planner plans |
   | `GroupMember.Read.All` | List the Microsoft 365 Groups a signed-in person belongs to, so plan discovery only shows plans for groups they're actually in |

2. Click **Grant admin consent** for the tenant and confirm both show
   consent granted.

No client ID, secret, encryption key or environment variable is needed —
unlike ClickUp, there is nothing else to configure. NoteIQ shows the
Microsoft Planner section in Account settings to every signed-in user as
soon as it's deployed.

**Grant consent before anyone tries to use it.** Because there is no flag
gating this, if the two permissions above aren't consented yet, a person who
opens Account settings or clicks **Send action items to Planner** hits a
real authorization error, not a hidden section. The UI is deliberately quiet
about a *background* discovery failure (see "Why plan discovery errors stay
quiet" below) but an explicit action still surfaces one plainly.

## Why plan discovery is scoped the way it is

Application permissions have no equivalent of "list the plans I can see" —
that shortcut only exists for a signed-in user's own delegated token, and
NoteIQ deliberately avoids adding a second, delegated Planner-specific
consent on top of the sign-in that already happened. Instead, discovery uses
the signed-in person's own Azure AD object ID (already known from sign-in)
to list *their* group memberships via `GroupMember.Read.All`, then lists each
of those groups' Planner plans via `Tasks.ReadWrite.All`. This is why
`GroupMember.Read.All` is requested at all: without it, the only alternative
would be `Group.Read.All`, which would let any signed-in NoteIQ user browse
*every* group's plan in the tenant, not just their own — a real access
boundary this design deliberately avoids.

A plan can also be added by pasting its ID directly (Account settings →
Microsoft Planner → Add a Planner plan), for a plan discovery didn't surface —
for example a plan on a Microsoft 365 Group the connected person belongs to
indirectly, or a plan type discovery does not walk. The plan's ID is the
value after `/plan/` in its Planner web URL.

## A caveat worth verifying before you rely on this

Microsoft's Planner Graph API surface has changed over time — most notably
the newer "Planner in Microsoft Teams" plans backed by `/planner/rosterPlans`
rather than a Microsoft 365 Group, which application-permission support has
rolled out to more gradually than classic Group-backed plans. This
integration targets classic, group-backed plans (`/groups/{id}/planner/plans`
and `/planner/plans/{id}`). Before depending on this in production, check
current Microsoft Learn documentation for whether `Tasks.ReadWrite.All`
(Application) covers the specific kind of plan your tenant uses — the
guidance above is accurate at the time of writing but this is an area
Microsoft continues to actively change.

## Why plan discovery errors stay quiet

Every signed-in user now sees the Microsoft Planner section, not just people
who opted in. Opening Account settings makes one background call to discover
that person's plans (`GET /api/planner/available-plans`). Before admin
consent is granted tenant-wide, that call 403s for everyone — and since it
fires automatically rather than from a click, it would otherwise put a red
error banner in front of every user just for opening a menu. That automatic
call fails quietly (the picker itself shows "Couldn't load plans. Click
Refresh to try again."); only an explicit **Refresh** click, or an actual
**Send action items to Planner** / **Add a Planner plan** action, surfaces a
visible error. This is a UI-side mitigation, not a substitute for granting
consent promptly — see the setup warning above.

## The link a created task points to

Microsoft Graph returns no URL on a `plannerTask` or `plannerPlan` — there is
nothing to read one from, and every *task*-level web link in circulation
(`tasks.office.com/{tenant}/Home/Task/{id}`, or Planner-for-the-web's
`planner.cloud.microsoft/webui/plan/{planId}/.../task/{id}`) is
reverse-engineered from "Copy link to task", not published by Microsoft, and
differs by whether the plan is Basic or Premium tier — a distinction Graph
doesn't expose either, so a client can't even pick the right one reliably.

Instead, every exported task and every row in "Existing tasks" carries a
**Teams deep link to its plan**:
`https://teams.microsoft.com/l/entity/com.microsoft.teamspace.tab.planner/mytasks?...&context={"subEntityId":"/v1/plan/{planId}"}`.
This is Microsoft's own documented Teams deep-link mechanism (the same
`teams.microsoft.com/l/entity/...` scheme NoteIQ's own Activity notifications
already use — see `app/activity.py`), not a reverse-engineered Planner web
URL, and it doesn't depend on plan tier. It opens Teams to the specific plan
via its first-party Planner tab (`com.microsoft.teamspace.tab.planner`); the
person finds the task there rather than following a link that might 404. The
construction in `app/planner.py`'s `plan_deep_link()` was checked
byte-for-byte against a real link copied from Teams' own "Copy link to plan",
and that comparison is a permanent regression test in `tests/test_planner.py`.

See [Create plannerTask](https://learn.microsoft.com/en-us/graph/api/planner-post-tasks),
[Update plannerTaskDetails](https://learn.microsoft.com/en-us/graph/api/plannertaskdetails-update)
(the `description` field, set with an `If-Match` etag), and the
[Graph permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference)
for `Tasks.ReadWrite.All` and `GroupMember.Read.All`.
