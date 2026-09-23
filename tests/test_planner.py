from app.planner import plan_deep_link
from tests.conftest import USER


def test_plan_deep_link_matches_a_real_link_copied_from_teams():
    """Byte-for-byte against a link a person copied from Teams' own "Copy
    link to plan", not just a plausible-looking guess -- this is the exact
    thing that was wrong before (a reverse-engineered, tier-dependent web
    URL), so it gets a permanent regression test rather than a one-time
    manual check.
    """
    assert plan_deep_link(
        "e781494f-04eb-465e-b6c7-09cca52f5631", "LxHN64HPJES9poQQRP6L1JgAFAR5"
    ) == (
        "https://teams.microsoft.com/l/entity/com.microsoft.teamspace.tab.planner/mytasks"
        "?tenantId=e781494f-04eb-465e-b6c7-09cca52f5631"
        "&webUrl=https%3A%2F%2Ftasks.teams.microsoft.com%2Fteamsui%2FpersonalApp%2Falltasklists"
        "&context=%7B%22subEntityId%22%3A%22%2Fv1%2Fplan%2FLxHN64HPJES9poQQRP6L1JgAFAR5%22%7D"
    )


class FakePlanner:
    def __init__(self):
        self.created = []
        self.deleted_task_ids = set()

    async def available_plans(self, user_id):
        assert user_id == USER
        return [{"id": "789", "name": "Design Reviews", "path": "Contoso Marketing"}]

    async def plan_name(self, plan_id):
        names = {"123": "Sprint Backlog", "456": "Bugs"}
        if plan_id not in names:
            raise ValueError("Microsoft Planner did not return a plan title.")
        return names[plan_id]

    async def create_task(self, plan_id, title, description):
        self.created.append((plan_id, title, description))
        task_id = str(len(self.created))
        return {"id": task_id, "url": f"https://tasks.office.com/tenant/Home/Task/{task_id}"}

    async def task_exists(self, task_id):
        return task_id not in self.deleted_task_ids

    async def list_tasks(self, plan_id):
        return [
            {
                "id": "t1",
                "title": "Existing task",
                "percent_complete": 50,
                "due_date": "2026-01-01T00:00:00Z",
                "bucket_name": "To do",
                "url": f"https://teams.microsoft.com/l/entity/x/y?plan={plan_id}",
            }
        ]


def add_plan(client, headers, plan_id="123"):
    response = client.post("/api/planner/plans", headers=headers, json={"plan_id": plan_id})
    assert response.status_code == 200
    return response


def test_planner_plan_management_and_task_export_are_private(client, store, signed_in, samples):
    fake = FakePlanner()
    client.app.state.planner = fake
    assert client.get("/api/planner", headers=signed_in).json() == {
        "plan_id": None,
        "plan_name": None,
        "plans": [],
    }
    assert (
        client.post(
            "/api/planner/plans", headers=signed_in, json={"plan_id": "no-such-plan"}
        ).status_code
        == 400
    )

    response = add_plan(client, signed_in)
    assert response.json() == {"plan_id": "123", "plan_name": "Sprint Backlog"}
    status = client.get("/api/planner", headers=signed_in).json()
    assert status["plan_name"] == "Sprint Backlog"
    assert status["plans"] == [
        {"plan_id": "123", "plan_name": "Sprint Backlog", "is_default": True}
    ]

    # Adding a second plan doesn't change the existing default.
    add_plan(client, signed_in, "456")
    status = client.get("/api/planner", headers=signed_in).json()
    assert status["plan_id"] == "123"
    assert {item["plan_id"] for item in status["plans"]} == {"123", "456"}

    store.save_meeting(USER, "Budget", {"meeting_id": "meeting", "insight": samples["insight"]})
    meeting_id = store.meetings(USER)[0]["id"]
    response = client.post(f"/api/meetings/{meeting_id}/planner", headers=signed_in, json={})
    assert response.json() == {"created": 3, "skipped": 0}
    assert len(fake.created) == 3
    assert fake.created[0][0] == "123"
    assert "Budget" in fake.created[0][2]
    assert client.post(
        f"/api/meetings/{meeting_id}/planner", headers=signed_in, json={}
    ).json() == {"created": 0, "skipped": 3}

    # Sending the same meeting to a different plan creates fresh tasks there.
    response = client.post(
        f"/api/meetings/{meeting_id}/planner", headers=signed_in, json={"plan_id": "456"}
    )
    assert response.json() == {"created": 3, "skipped": 0}
    assert fake.created[-1][0] == "456"

    # Deleting a task in Planner itself shouldn't leave it stuck as "already
    # sent": the next export should notice it's gone and re-create it.
    fake.deleted_task_ids.add("1")
    response = client.post(f"/api/meetings/{meeting_id}/planner", headers=signed_in, json={})
    assert response.json() == {"created": 1, "skipped": 2}

    assert (
        client.post(
            f"/api/meetings/{meeting_id}/planner",
            headers=signed_in,
            json={"plan_id": "no-such-plan"},
        ).status_code
        == 400
    )

    client.post("/api/planner/plans/default", headers=signed_in, json={"plan_id": "456"})
    assert client.get("/api/planner", headers=signed_in).json()["plan_id"] == "456"

    client.delete("/api/planner/plans/456", headers=signed_in)
    status = client.get("/api/planner", headers=signed_in).json()
    assert status["plan_id"] is None
    assert {item["plan_id"] for item in status["plans"]} == {"123"}

    assert client.get("/api/planner/available-plans", headers=signed_in).json() == {
        "plans": [{"id": "789", "name": "Design Reviews", "path": "Contoso Marketing"}]
    }


def test_planner_export_scopes_to_the_requested_provider(client, store, signed_in, samples):
    fake = FakePlanner()
    client.app.state.planner = fake
    add_plan(client, signed_in)
    store.save_meeting(
        USER,
        "Budget",
        {"meeting_id": "meeting", "insight": {**samples["insight"], "provider": "copilot"}},
    )
    store.save_meeting(
        USER,
        "Budget",
        {
            "meeting_id": "meeting",
            "insight": {
                "id": "openai:t1",
                "actionItems": [{"text": "OpenAI task", "ownerDisplayName": "Ada"}],
                "provider": "openai",
            },
        },
    )
    meeting_id = store.meetings(USER)[0]["id"]

    response = client.post(
        f"/api/meetings/{meeting_id}/planner", headers=signed_in, json={"provider": "openai"}
    )
    assert response.json() == {"created": 1, "skipped": 0}
    assert "OpenAI task" in fake.created[0][2]

    response = client.post(
        f"/api/meetings/{meeting_id}/planner", headers=signed_in, json={"provider": "copilot"}
    )
    assert response.json() == {"created": 3, "skipped": 0}

    # No provider filter: back to the original aggregate-everything behavior.
    response = client.post(f"/api/meetings/{meeting_id}/planner", headers=signed_in, json={})
    assert response.json() == {"created": 0, "skipped": 4}


def test_planner_requires_a_plan_before_export(client, store, signed_in):
    client.app.state.planner = FakePlanner()
    store.save_meeting(USER, "Mine", {"meeting_id": "mine"})
    meeting_id = store.meetings(USER)[0]["id"]
    assert (
        client.post(f"/api/meetings/{meeting_id}/planner", headers=signed_in, json={}).status_code
        == 409
    )


def test_planner_is_available_with_no_configuration_at_all(client, signed_in):
    """No PLANNER_ENABLED flag, no FakePlanner override -- just the app's
    default lifespan wiring against the `graph` fixture's plain AsyncMock
    (which returns [] from every .list call). Planner must work out of the
    box, exercising the real Planner class rather than a stand-in.
    """
    assert client.get("/api/planner", headers=signed_in).json() == {
        "plan_id": None,
        "plan_name": None,
        "plans": [],
    }
    assert client.get("/api/planner/available-plans", headers=signed_in).json() == {"plans": []}
    # Still correctly blocked, but for "no plan chosen", not "unavailable".
    assert client.post("/api/meetings/999/planner", headers=signed_in, json={}).status_code == 409


def test_planner_shows_what_already_exists(client, signed_in):
    """Retrieving a plan's current tasks, not just exporting new ones into it."""
    client.app.state.planner = FakePlanner()
    add_plan(client, signed_in)
    assert (
        client.get("/api/planner/tasks", headers=signed_in, params={"plan_id": "999"}).status_code
        == 400
    )
    response = client.get("/api/planner/tasks", headers=signed_in, params={"plan_id": "123"})
    assert response.status_code == 200
    assert response.json() == {
        "tasks": [
            {
                "id": "t1",
                "title": "Existing task",
                "percent_complete": 50,
                "due_date": "2026-01-01T00:00:00Z",
                "bucket_name": "To do",
                "url": "https://teams.microsoft.com/l/entity/x/y?plan=123",
            }
        ]
    }


def test_planner_disconnect_clears_saved_plans_and_tasks(client, store, signed_in, samples):
    client.app.state.planner = FakePlanner()
    add_plan(client, signed_in)
    store.save_meeting(USER, "Budget", {"meeting_id": "meeting", "insight": samples["insight"]})
    meeting_id = store.meetings(USER)[0]["id"]
    client.post(f"/api/meetings/{meeting_id}/planner", headers=signed_in, json={})
    assert store.planner_plans(USER)
    assert store.planner_default(USER) is not None

    assert client.post("/api/disconnect", headers=signed_in, json={}).json() == {
        "status": "disconnected"
    }
    assert store.planner_plans(USER) == []
    assert store.planner_default(USER) is None
