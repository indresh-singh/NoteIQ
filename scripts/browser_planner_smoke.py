"""Offline personal Planner consent/picker regression; run with uv --with playwright."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from app.config import Settings
from app.web import create_app
from scripts.browser_smoke import ORIGIN, TENANT, USER, FakeIdentity, make_router, sign_in


class PlannerIdentity(FakeIdentity):
    def acquire_token_by_auth_code_flow(self, flow, response):
        return {
            **super().acquire_token_by_auth_code_flow(flow, response),
            "access_token": "fake-microsoft-token",
            "scope": "User.Read Tasks.ReadWrite",
        }


def main():
    with TemporaryDirectory() as temporary:
        config = Settings(
            tenant_id=TENANT,
            graph_client_id=TENANT,
            graph_secret="test-secret",
            client_state="test-client-state-at-least-32-characters",
            public_url=ORIGIN,
            database=Path(temporary) / "test.sqlite3",
        )
        graph = AsyncMock()
        graph.list.return_value = []

        async def request(method, path, **kwargs):
            assert kwargs["access_token"] == "fake-delegated-token"
            if path == "/me/planner/plans":
                return {
                    "value": [
                        {
                            "id": "personal123",
                            "title": "My private plan",
                            "container": {"type": "user"},
                        }
                    ]
                }
            if path == "/planner/plans/personal123":
                return {"title": "My private plan"}
            return {"value": []}

        graph.request.side_effect = request
        with (
            patch("app.web.identity_client", PlannerIdentity),
            patch("app.web.delegated_token", AsyncMock(return_value="fake-delegated-token")),
            TestClient(create_app(config, graph, background=False), base_url=ORIGIN) as client,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND"])
            context = browser.new_context()
            errors = []
            context.route("**/*", make_router(client, errors))
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            sign_in(page, context, errors)
            page.locator(".account > summary").click()
            page.locator("#planner-connect").click()
            expect(page.locator("#planner-connect")).to_have_text(
                "Reconnect personal Planner", timeout=30000
            )
            expect(page.locator("#planner-plan-id")).to_contain_text("Personal / My private plan")
            page.locator("#planner-add-plan button[type=submit]").click()
            expect(page.locator("#planner-status")).to_contain_text('"My private plan"')
            assert client.app.state.store.planner_cache(USER)
            page.locator("#planner-tasks-wrap > summary").click()
            page.locator("#planner-refresh-tasks").click()
            expect(page.locator("#planner-tasks-empty")).to_be_visible()
            page.locator("#planner-disconnect").click()
            expect(page.locator("#planner-connect")).to_have_text("Connect personal Planner")
            assert client.app.state.store.planner_cache(USER) is None
            assert not errors, errors
            browser.close()
            print(
                "Planner browser check passed: consent popup, discovery, add, preview, disconnect."
            )


if __name__ == "__main__":
    main()
