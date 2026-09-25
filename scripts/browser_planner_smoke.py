"""Offline personal Planner consent/picker regression; run with uv --with playwright."""

import time
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
        discovered_plans = []
        discovery_failed = False

        async def request(method, path, **kwargs):
            assert kwargs["access_token"] == "fake-delegated-token"
            if path == "/me/planner/plans":
                if discovery_failed:
                    raise ValueError("Planner temporarily unavailable")
                return {"value": discovered_plans}
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
            # Real interaction renews a live session; ordinary polling does not.
            expiry = time.time() + 60
            with client.app.state.store.connect() as db:
                db.execute("UPDATE sessions SET expires=?", (expiry,))
            page.evaluate("lastSessionRenewal = -Infinity")
            with page.expect_response("**/api/session/renew") as renewal:
                page.locator(".account > summary").click()
            assert renewal.value.status == 200
            with client.app.state.store.connect() as db:
                renewed_expiry = db.execute("SELECT expires FROM sessions").fetchone()[0]
            assert renewed_expiry > expiry + 7 * 3600
            page.evaluate("refresh()")
            with client.app.state.store.connect() as db:
                assert db.execute("SELECT expires FROM sessions").fetchone()[0] == renewed_expiry
            expect(page.locator("#planner-plans-empty")).to_be_visible()
            expect(page.locator("#planner-plan-id")).to_be_disabled()
            expect(page.locator("#planner-add-plan button[type=submit]")).to_be_disabled()
            page.locator("#planner-connect").click()
            expect(page.locator("#planner-connect")).to_have_text(
                "Reconnect personal Planner", timeout=30000
            )
            expect(page.locator("#planner-plans-empty")).to_be_visible()
            expect(page.locator("#planner-plans-empty")).to_contain_text("create a plan")
            discovery_failed = True
            page.locator("#planner-refresh-plans").click()
            expect(page.locator("#planner-plan-id")).to_contain_text("Couldn't load plans")
            expect(page.locator("#planner-plans-empty")).to_be_hidden()
            discovery_failed = False
            discovered_plans.append(
                {
                    "id": "personal123",
                    "title": "My private plan",
                    "container": {"type": "user"},
                }
            )
            page.locator("#planner-refresh-plans").click()
            expect(page.locator("#planner-plan-id")).to_contain_text("Personal / My private plan")
            expect(page.locator("#planner-plans-empty")).to_be_hidden()
            page.locator("#planner-add-plan button[type=submit]").click()
            expect(page.locator("#planner-status")).to_contain_text('"My private plan"')
            assert client.app.state.store.planner_cache(USER)
            expect(page.locator("#planner-plan-id")).to_contain_text("No additional Planner plans")
            expect(page.locator("#planner-plans-empty")).to_be_hidden()
            page.locator("#planner-tasks-wrap > summary").click()
            page.locator("#planner-refresh-tasks").click()
            expect(page.locator("#planner-tasks-empty")).to_be_visible()
            page.locator("#planner-disconnect").click()
            expect(page.locator("#planner-connect")).to_have_text("Connect personal Planner")
            assert client.app.state.store.planner_cache(USER) is None
            assert not errors, errors
            browser.close()
            print(
                "Planner browser check passed: session renewal, empty/error states, "
                "consent popup, discovery, add, preview, disconnect."
            )


if __name__ == "__main__":
    main()
