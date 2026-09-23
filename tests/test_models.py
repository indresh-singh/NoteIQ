import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.models import InsightEvent, MeetingSync, TranscriptEvent, UserSync, parse_event

USER = UUID("11111111-1111-1111-1111-111111111111")


def test_insight_resource_round_trips_encoded_identifiers():
    event = InsightEvent.from_resource(
        f"/copilot/users/{USER}/onlineMeetings/meeting%2Fone/aiInsights/insight%20one"
    )

    assert event.meeting_id == "meeting/one"
    assert event.insight_id == "insight one"
    assert event.meeting_path.endswith("/onlineMeetings/meeting%2Fone")
    assert event.insight_path.endswith("/aiInsights/insight%20one")


@pytest.mark.parametrize(
    "resource",
    [
        f"/users/{USER}/onlineMeetings('meeting%2Fone')/transcripts('transcript%201')",
        f"/users/{USER}/onlineMeetings/meeting%2Fone/transcripts/transcript%201",
    ],
)
def test_transcript_resource_supports_both_graph_shapes(resource):
    event = TranscriptEvent.from_resource(resource)

    assert event.meeting_id == "meeting/one"
    assert event.transcript_id == "transcript 1"
    assert event.transcript_path.endswith(
        "/onlineMeetings/meeting%2Fone/transcripts/transcript%201"
    )


@pytest.mark.parametrize(
    "factory,resource",
    [
        (InsightEvent.from_resource, f"/copilot/users/{USER}/onlineMeetings/../aiInsights/i"),
        (TranscriptEvent.from_resource, f"/users/{USER}/onlineMeetings/m/transcripts/.."),
    ],
)
def test_resource_parsers_reject_path_traversal(factory, resource):
    with pytest.raises(ValueError, match="Invalid resource identifier"):
        factory(resource)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"type": "user_sync", "user_id": str(USER)}, UserSync),
        ({"user_id": str(USER), "meeting_id": "m"}, MeetingSync),
        (
            {"user_id": str(USER), "meeting_id": "m", "insight_id": "i"},
            InsightEvent,
        ),
        (
            {"user_id": str(USER), "meeting_id": "m", "transcript_id": "t"},
            TranscriptEvent,
        ),
    ],
)
def test_parse_event_dispatches_job_payloads(payload, expected):
    assert isinstance(parse_event(json.dumps(payload)), expected)


def test_parse_event_validates_required_identifiers():
    with pytest.raises(ValidationError):
        parse_event(json.dumps({"user_id": str(USER), "meeting_id": ""}))
