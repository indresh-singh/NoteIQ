from types import SimpleNamespace

from tests.conftest import USER


class FakeClickUp:
    def __init__(self):
        self.config = SimpleNamespace(
            clickup_client_id="clickup-client",
            clickup_redirect_uri="https://noteiq.test/clickup/callback",
        )
        self.created = []

    def encrypt(self, token):
        return "encrypted:" + token

    def decrypt(self, token):
        assert token == "encrypted:clickup-token"
        return "clickup-token"

    async def exchange(self, code):
        assert code == "clickup-code"
        return "clickup-token"

    async def workspaces(self, token):
        assert token == "clickup-token"
        return [{"id": "1", "name": "Demo Workspace"}]

    async def list_name(self, token, list_id):
        assert token == "clickup-token"
        if list_id != "123":
            raise ValueError("ClickUp did not return a List name.")
        return "Sprint Backlog"

    async def create_task(self, token, list_id, name, description):
        self.created.append((token, list_id, name, description))
        return {"id": str(len(self.created)), "url": "https://app.clickup.com/t/1"}


def connect(client, store, headers):
    fake = FakeClickUp()
    client.app.state.clickup = fake
    response = client.post("/api/clickup/connect", headers=headers, json={})
    assert response.status_code == 200
    assert response.json()["url"].startswith("https://noteiq.test/clickup/authorize?")
    state = response.json()["url"].split("state=")[1]
    authorize = client.get("/clickup/authorize", params={"state": state}, follow_redirects=False)
    assert authorize.status_code == 307
    assert authorize.headers["location"].startswith("https://app.clickup.com/api?")
    response = client.get("/clickup/callback", params={"state": state, "code": "clickup-code"})
    assert response.status_code == 200
    assert "clickup-token" not in response.text
    assert store.clickup(USER)["workspaces"] == [{"id": "1", "name": "Demo Workspace"}]
    return fake


def test_clickup_oauth_list_and_task_export_are_private(client, store, signed_in, samples):
    fake = connect(client, store, signed_in)
    assert client.get("/api/clickup", headers=signed_in).json()["connected"] is True
    assert (
        client.post("/api/clickup/list", headers=signed_in, json={"list_id": "999"}).status_code
        == 400
    )
    response = client.post("/api/clickup/list", headers=signed_in, json={"list_id": "123"})
    assert response.status_code == 200
    assert response.json() == {"list_id": "123", "list_name": "Sprint Backlog"}
    assert client.get("/api/clickup", headers=signed_in).json()["list_name"] == "Sprint Backlog"
    store.save_meeting(USER, "Budget", {"meeting_id": "meeting", "insight": samples["insight"]})
    meeting_id = store.meetings(USER)[0]["id"]
    response = client.post(f"/api/meetings/{meeting_id}/clickup", headers=signed_in, json={})
    assert response.json() == {"created": 3, "skipped": 0}
    assert len(fake.created) == 3
    assert "Budget" in fake.created[0][3]
    assert client.post(
        f"/api/meetings/{meeting_id}/clickup", headers=signed_in, json={}
    ).json() == {
        "created": 0,
        "skipped": 3,
    }
    assert client.post("/api/clickup/disconnect", headers=signed_in, json={}).json() == {
        "status": "disconnected"
    }


def test_clickup_requires_connection(client, store, signed_in):
    store.save_meeting(USER, "Mine", {"meeting_id": "mine"})
    assert (
        client.post("/api/clickup/list", headers=signed_in, json={"list_id": "123"}).status_code
        == 409
    )
    assert client.post("/api/meetings/999/clickup", headers=signed_in, json={}).status_code == 409


def test_clickup_oauth_returns_through_teams_popup(client, store, signed_in):
    client.app.state.clickup = FakeClickUp()
    response = client.post("/api/clickup/connect", headers=signed_in, json={"in_teams": True})
    assert response.json()["url"].startswith("https://noteiq.test/clickup/authorize?")
    state = response.json()["url"].split("state=")[1]
    response = client.get("/clickup/callback", params={"state": state, "code": "clickup-code"})
    assert response.status_code == 200
    assert 'data-in-teams="true"' in response.text
    assert "/static/clickup-complete.js" in response.text
