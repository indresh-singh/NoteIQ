"""Offline recurring-session UI check: uv run --with playwright python -m scripts.browser_occurrences_smoke."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from app.config import Settings
from app.models import Insight
from app.web import create_app
from scripts.browser_smoke import ORIGIN, TENANT, USER, FakeIdentity, make_router, sign_in


class AI:
    def __init__(self, config):
        pass

    async def summarize(self, key, subject, text):
        assert key.endswith("call:day23")
        assert text == "Transcript day 23"
        return Insight(id="openai:" + key, meetingNotes=[{"text": "Regenerated September 23"}])


def main():
    with TemporaryDirectory() as temporary:
        config = Settings(
            tenant_id=TENANT,
            graph_client_id=TENANT,
            graph_secret="test-secret",
            client_state="test-client-state-at-least-32-characters",
            public_url=ORIGIN,
            database=Path(temporary) / "test.sqlite3",
            openai_api_key="test",
        )
        graph = AsyncMock()
        graph.list.return_value = []
        graph.request.return_value = {
            "meetingType": "recurring",
            "participants": {"organizer": {"identity": {"user": {"id": USER}}}},
        }
        with (
            patch("app.web.identity_client", FakeIdentity),
            patch("app.web.settings", return_value=config),
            patch("app.transcripts.settings", return_value=config),
            patch("app.transcripts.OpenAI", AI),
            TestClient(create_app(config, graph, background=False), base_url=ORIGIN) as client,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND"])
            context = browser.new_context(viewport={"width": 1280, "height": 950})
            errors, exports = [], []
            recovery_status = 200
            router = make_router(client, errors)

            def route_request(route):
                url = route.request.url
                if url.endswith("/api/recover-meeting"):
                    route.fulfill(
                        status=recovery_status,
                        json={"found": 1, "queued": 1}
                        if recovery_status == 200
                        else {"detail": "Meeting not found for your organizer account."},
                    )
                elif url.endswith("/api/clickup"):
                    route.fulfill(
                        json={
                            "connected": True,
                            "lists": [{"list_id": "123", "list_name": "Backlog"}],
                        }
                    )
                elif url.endswith("/api/planner"):
                    route.fulfill(json={"plans": [{"plan_id": "123", "plan_name": "Backlog"}]})
                elif "/api/meetings/" in url and url.endswith(("/clickup", "/planner")):
                    exports.append(route.request.post_data_json)
                    route.fulfill(json={"created": 1, "skipped": 0})
                else:
                    router(route)

            context.route("**/*", route_request)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            sign_in(page, context, errors)
            store = client.app.state.store
            for day in (24, 22, 23):
                transcript = f"t{day}"
                local_id = store.save_transcript(
                    USER, "series", transcript, f"Transcript day {day}"
                )
                store.save_meeting(
                    USER,
                    "Daily Brief",
                    {
                        "meeting_id": "series",
                        "meeting_metadata": {"meeting_type": "recurring"},
                        "transcript": {
                            "id": transcript,
                            "local_id": local_id,
                            "callId": f"day{day}",
                            "contentCorrelationId": f"correlation-{day}",
                            "createdDateTime": f"2026-09-{day}T12:00:00Z",
                        },
                        "insight": {
                            "id": f"openai:day{day}",
                            "provider": "openai",
                            "occurrence_id": f"call:day{day}",
                            "meetingNotes": [{"text": f"Notes day {day}"}],
                            "actionItems": [{"text": f"Task day {day}"}],
                        },
                    },
                )
                for provider in ("copilot", "openrouter"):
                    store.save_meeting(
                        USER,
                        "Daily Brief",
                        {
                            "meeting_id": "series",
                            "insight": {
                                "id": f"{provider}:day{day}",
                                "provider": provider,
                                # Copilot joins by correlation; AI joins by call.
                                "contentCorrelationId": f"correlation-{day}",
                                "occurrence_id": None
                                if provider == "copilot"
                                else f"call:day{day}",
                                "endDateTime": f"2026-09-{day}T13:09:09Z"
                                if provider == "copilot"
                                else None,
                                "meetingNotes": [{"text": f"{provider} notes day {day}"}],
                            },
                        },
                    )
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator(".meeting-series")).to_have_count(1)
            sessions = page.locator(".meeting-occurrence")
            expect(sessions).to_have_count(3)
            expect(sessions.nth(0)).to_have_attribute("open", "")
            assert sessions.nth(1).get_attribute("open") is None
            expect(sessions.nth(0).locator("summary").first).to_contain_text("24")
            expect(sessions.nth(1).locator("summary").first).to_contain_text("23")
            latest = sessions.nth(0)
            session_date = latest.locator("time").inner_text()
            for provider, label in (("copilot", "Copilot"), ("openrouter", "OpenRouter")):
                latest.get_by_role("radio", name=label, exact=True).click()
                expect(latest.locator("time")).to_have_text(session_date)
                expect(latest.locator(".card-content")).to_contain_text(
                    f"{provider} notes day 24"
                )
                expect(latest.locator(".card-content")).not_to_contain_text("day 23")
            latest.get_by_role("radio", name="ChatGPT Enterprise", exact=True).click()
            expect(latest.locator("time")).to_have_text(session_date)
            sessions.nth(1).locator("summary").first.click()
            older = sessions.nth(1)
            older.get_by_role("button", name="Action items", exact=True).click()
            expect(older.locator(".card-content")).to_contain_text("Task day 23")
            expect(older.locator(".card-content")).not_to_contain_text("Task day 24")
            older.get_by_role("button", name="Send action items to ClickUp").click()
            older.get_by_role("button", name="Send action items to Planner").click()
            page.wait_for_function("document.querySelectorAll('.send-button:disabled').length >= 2")
            assert len(exports) == 2
            assert all(body["occurrence_id"] == "call:day23" for body in exports)
            older.get_by_role("button", name="Transcripts", exact=True).click()
            older.locator(".card-content summary").click()
            expect(older.locator(".transcript-text")).to_contain_text("Transcript day 23")
            older.get_by_role("button", name="Regenerate").click()
            expect(older.locator(".card-content")).to_contain_text("Regenerated September 23")
            expect(sessions.nth(0).locator(".card-content")).to_contain_text("Notes day 24")
            expect(older).to_have_attribute("open", "")

            # Recovery acknowledges a queued check, not new Copilot results.
            # Its outcome survives both a stale throttle flag and manual Refresh.
            page.evaluate("syncThrottled = true")
            page.locator("#meeting-url").fill("https://teams.microsoft.com/meet/123456789")
            page.get_by_role("button", name="Check meeting", exact=True).click()
            recovery = page.locator("#recovery-status")
            expect(recovery).to_contain_text("background check")
            expect(recovery).to_contain_text("existing results may stay unchanged")
            page.wait_for_function("!loading")
            page.evaluate("refresh()")
            expect(recovery).to_contain_text("background check")
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#refresh")).to_be_enabled()
            expect(recovery).to_contain_text("background check")
            recovery_status = 404
            page.get_by_role("button", name="Check meeting", exact=True).click()
            expect(recovery).to_contain_text("Meeting not found")
            page.evaluate("refresh()")
            expect(recovery).to_contain_text("Meeting not found")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            page.evaluate("signedOut()")
            expect(recovery).to_be_hidden()
            expect(recovery).to_have_text("")
            browser.close()
    print(
        "Recurring sessions: provider dates, scoped content/exports, regeneration and durable "
        "recovery outcomes passed."
    )


if __name__ == "__main__":
    main()
