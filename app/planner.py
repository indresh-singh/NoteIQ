"""Microsoft Planner integration, reached over the same Graph app as
transcripts, insights and Activity notifications -- no separate OAuth app
or token storage the way app/clickup.py needs, because the identity is
already the one that signed into NoteIQ.

Application permissions have no equivalent of "list the plans I can see":
that shortcut (`/me/planner/plans`) only exists for a signed-in user's own
delegated token. Discovery instead walks the Microsoft 365 Groups the
connected person actually belongs to (GroupMember.Read.All) and lists each
one's plan (Tasks.ReadWrite.All) -- the same workspace/space/list cascade
app/clickup.py's available_lists() does, minus the OAuth round trip.
"""

import json
import logging
from urllib.parse import quote, urlencode

import httpx

from app.config import Settings
from app.graph_client import GraphBusy, GraphClient, busy_from

log = logging.getLogger(__name__)

# Planner's own first-party Teams app: a fixed, Microsoft-published identifier,
# not something that varies by tenant or plan (unlike tenant_id/plan_id below,
# nothing here is guessed or hardcoded per-deployment).
PLANNER_TEAMS_APP_ID = "com.microsoft.teamspace.tab.planner"
PLANNER_TEAMS_ENTITY_ID = "mytasks"
PLANNER_TEAMS_WEB_FALLBACK = "https://tasks.teams.microsoft.com/teamsui/personalApp/alltasklists"


def plan_deep_link(tenant_id: object, plan_id: str) -> str:
    """A Teams deep link that opens one specific plan in Planner's personal tab.

    This is Microsoft's own documented Teams deep-link mechanism
    (teams.microsoft.com/l/entity/<appId>/<entityId>?context=...) -- the same
    scheme NoteIQ's own Activity notifications already use (see
    app/activity.py) -- not a reverse-engineered Planner web URL. The
    `context.subEntityId` is what tells the Planner tab which plan to open;
    tenant_id and plan_id are the only two things that vary per call. This
    construction was verified byte-for-byte against a real link copied from
    Teams' own "Copy link to plan".
    """
    context = json.dumps({"subEntityId": f"/v1/plan/{plan_id}"}, separators=(",", ":"))
    query = urlencode(
        {"tenantId": str(tenant_id), "webUrl": PLANNER_TEAMS_WEB_FALLBACK, "context": context}
    )
    return f"https://teams.microsoft.com/l/entity/{PLANNER_TEAMS_APP_ID}/{PLANNER_TEAMS_ENTITY_ID}?{query}"


async def _call(coro):
    """Run one Graph call, turning failures into the ValueError the web layer expects."""
    try:
        return await coro
    except httpx.HTTPStatusError as error:
        busy = busy_from(error)
        if busy is not None:
            raise busy from None
        status = error.response.status_code
        if status in (401, 403):
            raise ValueError(
                "NoteIQ isn't authorized for Microsoft Planner. An administrator needs to "
                "grant it the Planner permissions described in the setup notes."
            ) from None
        if status == 404:
            raise ValueError("That Planner plan or task couldn't be found.") from None
        raise ValueError("Microsoft Planner could not complete this request. Try again.") from None
    except httpx.HTTPError:
        raise ValueError("Unable to reach Microsoft Planner. Try again.") from None


class Planner:
    def __init__(self, config: Settings, graph: GraphClient):
        self.config = config
        self.graph = graph

    async def available_plans(self, user_id: str) -> list[dict]:
        """Plans belonging to the Microsoft 365 Groups this person is a member of.

        Skips a group's plan lookup instead of failing the whole picker over
        one restricted or planless group, mirroring ClickUp's available_lists.
        """
        groups = await _call(
            self.graph.list(
                f"/users/{quote(user_id, safe='')}/memberOf/microsoft.graph.group"
                "?$select=id,displayName,groupTypes"
            )
        )
        found = []
        for group in groups:
            if "Unified" not in (group.get("groupTypes") or []):
                continue  # Only Microsoft 365 Groups can own a Planner plan.
            group_id = group.get("id")
            if not group_id:
                continue
            try:
                plans = await _call(
                    self.graph.list(f"/groups/{quote(group_id, safe='')}/planner/plans")
                )
            except ValueError as error:
                log.warning(
                    "Planner group skipped during plan discovery group_name=%r error=%s",
                    group.get("displayName"),
                    error,
                )
                continue
            path = group.get("displayName") or "Group"
            for plan in plans:
                if plan.get("id"):
                    found.append({"id": plan["id"], "name": plan.get("title", ""), "path": path})
        return found

    async def plan_name(self, plan_id: str) -> str:
        """Look up a plan's title directly, for a plan ID pasted rather than picked."""
        plan = await _call(self.graph.request("GET", f"/planner/plans/{quote(plan_id, safe='')}"))
        name = plan.get("title")
        if not isinstance(name, str) or not name:
            raise ValueError("Microsoft Planner did not return a plan title.")
        return name

    async def create_task(self, plan_id: str, title: str, description: str) -> dict:
        task = await _call(
            self.graph.request(
                "POST", "/planner/tasks", json={"planId": plan_id, "title": title[:255]}
            )
        )
        task_id = task["id"]
        try:
            details = await _call(
                self.graph.request("GET", f"/planner/tasks/{quote(task_id, safe='')}/details")
            )
            await _call(
                self.graph.request(
                    "PATCH",
                    f"/planner/tasks/{quote(task_id, safe='')}/details",
                    headers={"If-Match": details["@odata.etag"]},
                    json={"description": description[:20_000]},
                )
            )
        except (ValueError, GraphBusy) as error:
            # The task itself was created; missing its notes is not a failed
            # export, so this is logged and swallowed rather than raised --
            # throttling included, or a retry would create the task twice.
            log.warning("Planner task description not set task_id=%s error=%s", task_id[:12], error)
        return {
            "id": task_id,
            # A link to the plan, not the specific task: Graph returns no URL
            # for a plannerTask, and every *task*-level web link format in
            # circulation is reverse-engineered and differs by plan tier (see
            # docs/planner.md). A *plan*-level Teams deep link has no such
            # problem -- it's Microsoft's own documented mechanism -- so the
            # person lands directly on the right plan and finds the task
            # there, rather than a generic app-root link or a guessed,
            # possibly-broken task URL.
            "url": plan_deep_link(self.config.tenant_id, plan_id),
        }

    async def task_exists(self, task_id: str) -> bool:
        """Whether a previously exported task is still present in Planner.

        A plain call would turn a 404 (task deleted in Planner) into a generic
        ValueError, indistinguishable from a real failure. This treats 404 as
        a normal "no longer exists" result, exactly like ClickUp's
        task_exists, so callers re-export instead of erroring out.
        """
        try:
            await self.graph.request("GET", f"/planner/tasks/{quote(task_id, safe='')}")
            return True
        except httpx.HTTPStatusError as error:
            busy = busy_from(error)
            if busy is not None:
                raise busy from None
            if error.response.status_code == 404:
                return False
            raise ValueError(
                "Microsoft Planner could not complete this request. Try again."
            ) from None
        except httpx.HTTPError:
            raise ValueError("Unable to reach Microsoft Planner. Try again.") from None

    async def list_tasks(self, plan_id: str) -> list[dict]:
        """Tasks already in a plan, for showing what exists there before exporting more."""
        tasks = await _call(self.graph.list(f"/planner/plans/{quote(plan_id, safe='')}/tasks"))
        buckets = await _call(self.graph.list(f"/planner/plans/{quote(plan_id, safe='')}/buckets"))
        bucket_names = {
            bucket["id"]: bucket.get("name", "") for bucket in buckets if bucket.get("id")
        }
        url = plan_deep_link(self.config.tenant_id, plan_id)
        return [
            {
                "id": task["id"],
                "title": task.get("title", ""),
                "percent_complete": task.get("percentComplete", 0),
                "due_date": task.get("dueDateTime"),
                "bucket_name": bucket_names.get(task.get("bucketId"), ""),
                "url": url,
            }
            for task in tasks
            if task.get("id")
        ]
