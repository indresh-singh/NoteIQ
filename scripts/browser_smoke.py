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


def make_router(client, errors, meetings_hits=None):
    """A Playwright route handler that serves noteiq.test from `client` in-process
    and fakes just enough of login.microsoftonline.com for the popup sign-in flow.
    """

    def route_request(route):
        request = route.request
        url = urlsplit(request.url)
        if url.hostname == "login.microsoftonline.com":
            state = parse_qs(url.query)["state"][0]
            destination = (
                ORIGIN + "/auth/callback?" + urlencode({"state": state, "code": "simulated-code"})
            )
            route.fulfill(
                content_type="text/html",
                body='<meta http-equiv="refresh" content="0;url='
                + html.escape(destination, quote=True)
                + '">',
            )
        elif url.hostname == "noteiq.test":
            if meetings_hits is not None and url.path == "/api/meetings":
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

    return route_request


def sign_in(page, context, errors, welcome_screenshot=None):
    """Click through the popup sign-in and land on the (empty) meetings workspace."""
    page.goto(ORIGIN)
    expect(page.get_by_role("button", name="Connect Microsoft 365")).to_be_visible()
    if welcome_screenshot:
        page.screenshot(path=welcome_screenshot, full_page=True)
    page.get_by_role("button", name="Connect Microsoft 365").click()
    try:
        expect(page.get_by_role("heading", name="Your meetings")).to_be_visible(timeout=30000)
    except AssertionError:
        for opened in context.pages:
            print("Browser page:", urlsplit(opened.url).path, opened.locator("body").inner_text())
        print("JavaScript errors:", errors)
        raise


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
            context.route("**/*", make_router(client, errors, meetings_hits))
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            sign_in(page, context, errors, welcome_screenshot="/tmp/noteiq-welcome.png")
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


def check_meetings_ui_polish():
    """Tab styling, the missed-meeting form's position, and meeting pagination.

    A fresh app/store/browser context, independent of main()'s: seeding six
    meetings here must not disturb main()'s timing-sensitive chase assertions.
    """
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
            TestClient(create_app(config, graph, background=False), base_url=ORIGIN) as client,
            sync_playwright() as playwright,
        ):
            browser = playwright.chromium.launch(args=["--host-resolver-rules=MAP * ~NOTFOUND"])
            context = browser.new_context(viewport={"width": 1280, "height": 950})
            errors = []
            context.route("**/*", make_router(client, errors))
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            sign_in(page, context, errors)

            # --- Tabs must read as tabs, not as two ordinary buttons ----------
            meetings_tab = page.locator("#meetings-tab")
            upload_tab = page.locator("#upload-tab")
            selected_style = meetings_tab.evaluate(
                "el => { const s = getComputedStyle(el); "
                "return {border: s.borderWidth, bg: s.backgroundColor}; }"
            )
            unselected_style = upload_tab.evaluate(
                "el => { const s = getComputedStyle(el); "
                "return {border: s.borderWidth, bg: s.backgroundColor}; }"
            )
            assert unselected_style["border"] in ("0px", ""), (
                f"unselected tab still has a visible border: {unselected_style}"
            )
            assert selected_style["bg"] != unselected_style["bg"], (
                "the selected tab is not visually distinguished from the unselected one"
            )
            upload_tab.click()
            expect(page.locator("#upload-panel")).to_be_visible()
            expect(page.locator("#meetings-panel")).to_be_hidden()
            meetings_tab.click()
            expect(page.locator("#meetings-panel")).to_be_visible()

            # --- The missed-meeting recovery form sits below the meetings list,
            # not above it, regardless of how long that list is. ---------------
            store = client.app.state.store
            insight = Insight.model_validate(
                json.loads((ROOT / "tests/fixtures/insight.json").read_text())
            )
            for index in range(5):
                subject = f"Standup {index}"
                store.save_meeting(
                    USER,
                    subject,
                    {
                        "meeting_id": f"meeting-{index}",
                        "insight": insight.model_dump(mode="json"),
                        "card": build_card(insight, subject),
                    },
                )
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#meetings article")).to_have_count(5)
            expect(page.locator(".pagination")).to_have_count(0)
            meetings_box = page.locator("#meetings").bounding_box()
            recover_box = page.locator("#recover-meeting").bounding_box()
            assert recover_box["y"] >= meetings_box["y"] + meetings_box["height"] - 1, (
                "the missed-meeting form is not below the meetings list: "
                f"recover top={recover_box['y']}, meetings bottom="
                f"{meetings_box['y'] + meetings_box['height']}"
            )

            # --- A sixth meeting introduces pagination, five to a page --------
            store.save_meeting(
                USER,
                "Standup 5",
                {
                    "meeting_id": "meeting-5",
                    "insight": insight.model_dump(mode="json"),
                    "card": build_card(insight, "Standup 5"),
                },
            )
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.locator("#meetings article")).to_have_count(5)
            expect(page.get_by_text("Page 1 of 2", exact=True)).to_be_visible()
            previous_button = page.get_by_role("button", name="Previous", exact=True)
            next_button = page.get_by_role("button", name="Next", exact=True)
            assert previous_button.is_disabled(), "Previous should be disabled on page 1"
            assert not next_button.is_disabled()
            next_button.click()
            expect(page.locator("#meetings article")).to_have_count(1)
            expect(page.get_by_text("Page 2 of 2", exact=True)).to_be_visible()
            assert next_button.is_disabled(), "Next should be disabled on the last page"
            previous_button.click()
            expect(page.locator("#meetings article")).to_have_count(5)
            expect(page.get_by_text("Page 1 of 2", exact=True)).to_be_visible()
            # Pagination survives an ordinary re-render (e.g. Refresh) instead
            # of silently resetting the page a person navigated to.
            next_button.click()
            expect(page.get_by_text("Page 2 of 2", exact=True)).to_be_visible()
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(page.get_by_text("Page 2 of 2", exact=True)).to_be_visible()

            page.screenshot(path="/tmp/noteiq-pagination.png", full_page=True)

            # --- None of the above overflows a phone-width viewport ------------
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            expect(page.locator(".pagination")).to_be_visible()
            expect(page.locator("#recover-meeting")).to_be_visible()
            page.screenshot(path="/tmp/noteiq-pagination-mobile.png", full_page=True)

            assert not errors, errors
            browser.close()
            print("Browser check passed: tab styling, form position, pagination.")


if __name__ == "__main__":
    main()
    check_meetings_ui_polish()
