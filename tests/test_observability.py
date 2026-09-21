"""Diagnostic logging that separates Microsoft's publication lag from ours."""

import logging
import re
from datetime import datetime, timedelta, timezone

from app.insights import process_insight
from app.models import MeetingSync, age_seconds
from app.store import digest
from app.sync import sync_meeting
from tests.conftest import USER
from tests.test_insights import clients


def test_age_seconds_parses_graph_timestamps_and_rejects_junk():
    assert age_seconds(None) is None
    assert age_seconds("") is None
    assert age_seconds("not-a-timestamp") is None
    recent = datetime.now(timezone.utc) - timedelta(seconds=90)
    assert 85 < age_seconds(recent.strftime("%Y-%m-%dT%H:%M:%SZ")) < 95
    # A naive timestamp is read as UTC rather than skewing the measurement.
    assert 85 < age_seconds(recent.replace(tzinfo=None)) < 95


async def test_first_sighting_is_logged_once_per_artifact(store, graph, caplog):
    store.save_meeting(USER, "Mine", {"meeting_id": "mine"})
    graph.list.return_value = [{"id": "artifact-1"}]
    with caplog.at_level(logging.INFO):
        assert await sync_meeting(MeetingSync(user_id=USER, meeting_id="mine"), graph, store) == (
            "SYNCED"
        )
    tag = digest("mine")[:8]
    assert f"Artifact first visible user={USER} meeting={tag} kind=insight count=1" in caplog.text
    assert f"meeting={tag} kind=insight available=1 new=1" in caplog.text

    # Once the insight is stored it is no longer new, so its marker stops. The
    # transcript is still unsaved here, so its own marker must keep firing.
    store.save_meeting(USER, "Mine", {"meeting_id": "mine", "insight": {"id": "artifact-1"}})
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await sync_meeting(MeetingSync(user_id=USER, meeting_id="mine"), graph, store)
    assert f"Artifact first visible user={USER} meeting={tag} kind=insight" not in caplog.text
    assert f"Artifact first visible user={USER} meeting={tag} kind=transcript" in caplog.text
    assert f"meeting={tag} kind=insight available=1 new=0" in caplog.text


async def test_insight_log_reports_publication_lag_without_content(samples, store, caplog):
    graph, store, event = clients(samples, store)
    with caplog.at_level(logging.INFO):
        assert await process_insight(event, graph, store) == "SAVED"
    lag = re.search(r"publish_lag_s=(\d+)", caplog.text)
    assert lag, caplog.text
    # The fixture's endDateTime is in the past, so the lag must be positive.
    assert int(lag[1]) > 0
    assert f"meeting={digest(event.meeting_id)[:8]}" in caplog.text
    # Identifiers stay hashed and content never reaches the log.
    assert event.meeting_id not in caplog.text
    assert "Pricing approach agreed" not in caplog.text


def test_webhook_log_names_the_resource_kind(client, store, samples, caplog):
    with caplog.at_level(logging.INFO):
        response = client.post("/api/graph/notifications", json=samples["notification"])
    assert response.status_code == 202
    assert "Graph webhook received=1 enrolled=1 kinds=insight" in caplog.text
