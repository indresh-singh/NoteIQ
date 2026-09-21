"""Offline browser check: uv run --with playwright python -m scripts.browser_smoke.

Microsoft sign-in and Graph are simulated. No external request is sent by this check.
Install its browser once with: uv run --with playwright python -m playwright install chromium
"""

import html
import json
import secrets
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from app.adaptive_cards import build_card
from app.config import ROOT, Settings
from app.models import Insight
from app.web import create_app

USER = "11111111-1111-1111-1111-111111111111"
TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ORIGIN = "https://noteiq.test"


class FakeIdentity:
    def __init__(self, **kwargs):
        pass

    def initiate_auth_code_flow(self, **kwargs):
        state = secrets.token_urlsafe(32)
        return {
            "state": state,
            "auth_uri": "https://login.microsoftonline.com/authorize?"
            + urlencode({"state": state}),
        }

    def acquire_token_by_auth_code_flow(self, flow, response):
        assert flow["state"] == response["state"]
        return {"id_token_claims": {"oid": USER, "tid": TENANT, "name": "Demo Organizer"}}


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
        with (
            patch("app.web.identity_client", FakeIdentity),
            TestClient(
                create_app(config, graph, background=False),
                base_url=ORIGIN,
            ) as client,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND"])
            context = browser.new_context(viewport={"width": 1280, "height": 950})
            errors = []
            meetings_hits = []

            def route_request(route):
                request = route.request
                url = urlsplit(request.url)
                if url.hostname == "login.microsoftonline.com":
                    state = parse_qs(url.query)["state"][0]
                    destination = (
                        ORIGIN
                        + "/auth/callback?"
                        + urlencode({"state": state, "code": "simulated-code"})
                    )
                    route.fulfill(
                        content_type="text/html",
                        body='<meta http-equiv="refresh" content="0;url='
                        + html.escape(destination, quote=True)
                        + '">',
                    )
                elif url.hostname == "noteiq.test":
                    if url.path == "/api/meetings":
                        meetings_hits.append(time.monotonic())
                    response = client.request(
                        request.method,
                        request.url,
                        headers=request.headers,
                        content=request.post_data,
                        follow_redirects=False,
                    )
                    if response.is_redirect:
                        # Playwright intercepts only the first request of an HTTP redirect chain.
                        # A fresh document navigation keeps every simulated hop intercepted.
                        route.fulfill(
                            content_type="text/html",
                            body='<meta http-equiv="refresh" content="0;url='
                            + html.escape(response.headers["location"], quote=True)
                            + '">',
                        )
                    else:
                        route.fulfill(
                            status=response.status_code,
                            headers=dict(response.headers),
                            body=response.content,
                        )
                else:
                    errors.append("Unexpected network request: " + url.hostname)
                    route.abort()

            context.route("**/*", route_request)
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(ORIGIN)
            expect(page.get_by_role("button", name="Connect Microsoft 365")).to_be_visible()
            page.screenshot(path="/tmp/noteiq-welcome.png", full_page=True)
            page.get_by_role("button", name="Connect Microsoft 365").click()
            try:
                expect(page.get_by_role("heading", name="Your meetings")).to_be_visible(
                    timeout=30000
                )
            except AssertionError:
                for opened in context.pages:
                    print(
                        "Browser page:",
                        urlsplit(opened.url).path,
                        opened.locator("body").inner_text(),
                    )
                print("JavaScript errors:", errors)
                raise
            expect(page.get_by_text("Ready for your next meeting", exact=True)).to_be_visible()
            store = client.app.state.store
            assert store.users() == [USER]
            insight = Insight.model_validate(
                json.loads((ROOT / "tests/fixtures/insight.json").read_text())
            )
            store.save_meeting(
                USER,
                "FY27 Budget Review",
                {
                    "meeting_id": "demo-meeting",
                    "insight": insight.model_dump(mode="json"),
                    "card": build_card(insight, "FY27 Budget Review"),
                },
            )
            transcript_id = store.save_transcript(
                USER,
                "demo-meeting",
                "transcript",
                "WEBVTT\n00:01 --> 00:02\n<v Ada>Demo transcript</v>",
            )
            store.save_meeting(
                USER,
                "FY27 Budget Review",
                {
                    "meeting_id": "demo-meeting",
                    "transcript": {"id": "transcript", "local_id": transcript_id},
                },
            )
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.get_by_role("heading", name="FY27 Budget Review")).to_be_visible()
            expect(page.locator(".card-content details[open]")).to_have_count(0)
            page.locator(".card-content summary").first.click()
            expect(page.locator(".card-content")).to_contain_text("Launch date remains unchanged")
            page.get_by_role("button", name="Action items", exact=True).click()
            expect(page.locator(".card-content")).to_contain_text(
                "Submit the revised commercial proposal"
            )
            expect(page.locator(".card-content")).not_to_contain_text(
                "Launch date remains unchanged"
            )
            page.screenshot(path="/tmp/noteiq-meetings.png", full_page=True)
            page.get_by_role("button", name="Transcripts", exact=True).click()
            page.get_by_text("View transcript", exact=True).click()
            expect(page.locator(".transcript-text")).to_contain_text("Ada: Demo transcript")
            expect(page.locator("#notification-status")).to_contain_text("Teams Activity")
            expect(page.locator("#chat-picker")).to_have_count(0)

            # A sync that queues work must re-read within seconds rather than
            # waiting for the 15-second interval. The worker is disabled here,
            # so the card is saved by hand the way process_insight would.
            graph.list.return_value = [{"id": "chase-t", "meetingId": "chase-meeting"}]
            meetings_hits.clear()
            started = time.monotonic()
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#status")).to_contain_text("Found new activity")
            store.save_meeting(
                USER,
                "Chase Arrival",
                {
                    "meeting_id": "chase-meeting",
                    "insight": insight.model_dump(mode="json"),
                    "card": build_card(insight, "Chase Arrival"),
                },
            )
            expect(page.get_by_role("heading", name="Chase Arrival")).to_be_visible(timeout=8000)
            assert time.monotonic() - started < 8, "the chase did not beat the 15-second interval"
            page.wait_for_timeout(6000)
            # Within 8s: the click's own read plus chase passes at 2s and 5s. The
            # 15-second interval can add at most one, so three proves a chase ran.
            early = [hit for hit in meetings_hits if hit - started < 8]
            assert len(early) >= 3, f"expected chase re-reads, saw {len(early)}"

            # A newer chase cancels the older ones instead of stacking timers.
            meetings_hits.clear()
            for _ in range(3):
                page.get_by_role("button", name="Refresh", exact=True).click()
                expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
                page.wait_for_timeout(1000)
            page.wait_for_timeout(12000)
            # Only the newest chase survives: three click reads, four passes and at
            # most one interval tick. Three stacked chases would add eight more.
            assert len(meetings_hits) <= 10, f"chases are stacking: {len(meetings_hits)} reads"

            page.reload()
            expect(page.get_by_role("heading", name="FY27 Budget Review")).to_be_visible(
                timeout=15000
            )
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            # Exercise the Teams SDK path as well as the ordinary browser path.
            teams_user = USER
            page.route("**/static/vendor/teams.min.js", lambda route: route.fulfill(
                content_type="application/javascript",
                body="window.microsoftTeams={app:{initialize:async()=>{},"
                "getContext:async()=>({app:{theme:'default'},user:{id:"
                + json.dumps(teams_user)
                + "}}),registerOnThemeChangeHandler:()=>{},notifyAppLoaded:()=>{},notifySuccess:()=>{}}};",
            ))
            page.reload()
            expect(page.get_by_role("heading", name="FY27 Budget Review")).to_be_visible()
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
            teams_user = "99999999-9999-9999-9999-999999999999"
            page.reload()
            expect(page.locator("#error")).to_contain_text("different Microsoft account")
            expect(page.locator("#workspace")).to_be_hidden()
            teams_user = USER
            page.evaluate("value => sessionStorage.setItem('noteiq-session', value)", store.session(USER))
            page.reload()
            expect(page.get_by_role("heading", name="FY27 Budget Review")).to_be_visible()
            page.get_by_text("Account settings", exact=True).click()
            page.on("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Disconnect and delete my NoteIQ cards").click()
            expect(page.get_by_role("button", name="Connect Microsoft 365")).to_be_visible()
            assert store.users() == []
            assert store.meetings(USER) == []
            assert not errors, errors
            browser.close()
            print(
                "Browser check passed: popup sign-in, enrollment, cards, buttons, "
                "refresh chase, reload, mobile layout, disconnect."
            )


if __name__ == "__main__":
    main()
