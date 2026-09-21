import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from microsoft_teams.cards import AdaptiveCard

from app.adaptive_cards import build_card
from app.config import settings
from app.insights import process_insight
from app.models import Insight, InsightEvent


def clients(samples, store):
    graph = AsyncMock()
    graph.request.side_effect = [samples["meeting"], samples["insight"]]
    event = InsightEvent.from_resource(samples["notification"]["value"][0]["resource"])
    return graph, store, event


def test_preserves_copilot_content_and_missing_owner(samples, store):
    card = build_card(Insight.model_validate(samples["insight"]), "Budget")
    AdaptiveCard.model_validate(card)
    text = json.dumps(card)
    assert "Owner not specified" in text
    assert "Launch date remains unchanged." in text
    assert "Submit the revised commercial proposal." in text
    assert "dueDate" not in text


def test_action_item_due_date_only_rendered_when_present():
    with_due = build_card(
        Insight(
            id="i",
            actionItems=[
                {"text": "Send proposal", "ownerDisplayName": "Ada", "dueDate": "2026-09-20"}
            ],
        ),
        "Budget",
    )
    assert "Due: 2026-09-20" in json.dumps(with_due)

    without_due = build_card(
        Insight(id="i", actionItems=[{"text": "Send proposal", "ownerDisplayName": "Ada"}]),
        "Budget",
    )
    assert "Due:" not in json.dumps(without_due)


def test_empty_card_not_sent():
    assert build_card(Insight(id="empty"), "Budget") is None
    assert (
        build_card(Insight(id="empty", actionItems=[{"ownerDisplayName": "Sarah"}]), "Budget")
        is None
    )


def test_oversized_card_is_reviewed():
    with pytest.raises(ValueError, match="size limit"):
        build_card(Insight(id="big", meetingNotes=[{"text": "x" * 25_000}]), "Budget")


async def test_delivery_and_content_free_logs(samples, store, caplog):
    graph, store, event = clients(samples, store)
    with caplog.at_level(logging.INFO):
        assert await process_insight(event, graph, store) == "SAVED"
    assert len(store.meetings(str(event.user_id))) == 1
    assert "SAVED" in caplog.text
    assert "latency_ms=" in caplog.text
    assert "Pricing approach agreed" not in caplog.text


async def test_attendee_event_never_receives_or_forwards_card(samples, store):
    samples["meeting"]["participants"]["organizer"]["identity"]["user"]["id"] = (
        "99999999-9999-9999-9999-999999999999"
    )
    graph, store, event = clients(samples, store)
    assert await process_insight(event, graph, store) == "SKIPPED_NOT_ORGANIZER"
    assert store.meetings(str(event.user_id)) == []
    assert graph.request.await_count == 1


async def test_unknown_organizer_needs_review(samples, store):
    samples["meeting"]["participants"] = {}
    graph, store, event = clients(samples, store)
    assert await process_insight(event, graph, store) == "NEEDS_REVIEW"
    assert store.meetings(str(event.user_id)) == []


async def test_empty_insight_skips_delivery(samples, store):
    samples["insight"] = {"id": "empty"}
    graph, store, event = clients(samples, store)
    assert await process_insight(event, graph, store) == "SKIPPED_EMPTY"
    assert store.meetings(str(event.user_id)) == []


async def test_disconnected_user_is_skipped(samples, store):
    graph, store, event = clients(samples, store)
    store.disconnect(str(event.user_id))
    assert await process_insight(event, graph, store) == "SKIPPED_NOT_ENROLLED"
    graph.request.assert_not_awaited()


async def test_copilot_insight_is_processed_with_external_summary_service(monkeypatch, samples, store):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()
    graph, store, event = clients(samples, store)
    assert await process_insight(event, graph, store) == "SAVED"
    assert len(store.meetings(str(event.user_id))) == 1


@pytest.mark.parametrize("code", [404, 429, 500, 503])
async def test_temporary_failure_reaches_queue_retry(samples, store, code):
    graph, store, event = clients(samples, store)
    response = httpx.Response(code, request=httpx.Request("GET", "https://graph.microsoft.com"))
    graph.request.side_effect = httpx.HTTPStatusError(
        "sensitive-response", request=response.request, response=response
    )
    with pytest.raises(RuntimeError, match="Retry meeting follow-up"):
        await process_insight(event, graph, store)
    assert store.meetings(str(event.user_id)) == []


async def test_permanent_failure_is_logged_without_sensitive_body(samples, store, caplog):
    graph, store, event = clients(samples, store)
    response = httpx.Response(403, request=httpx.Request("GET", "https://graph.microsoft.com"))
    graph.request.side_effect = httpx.HTTPStatusError(
        "sensitive-response", request=response.request, response=response
    )
    with caplog.at_level(logging.INFO):
        assert await process_insight(event, graph, store) == "FAILED_PERMANENT"
    assert "sensitive-response" not in caplog.text
