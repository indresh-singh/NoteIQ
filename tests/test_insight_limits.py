from copy import deepcopy

from app.occurrences import present_meeting, select_session
from app.web import actions


def test_saved_actions_are_capped_per_session_in_views_and_exports():
    content = {
        "meeting_id": "series",
        "meeting_metadata": {"meeting_type": "recurring"},
        "transcripts": [],
        "insights": [],
    }
    for day in (23, 24):
        content["transcripts"].append(
            {
                "transcript": {
                    "id": f"t{day}",
                    "callId": str(day),
                    "createdDateTime": f"2026-09-{day}T12:00:00Z",
                }
            }
        )
        for provider in ("openai", "openrouter"):
            content["insights"].append(
                {
                    "insight": {
                        "id": f"{provider}:{day}",
                        "provider": provider,
                        "occurrence_id": f"call:{day}",
                        "actionItems": [{"text": f"Task {i}"} for i in range(12)],
                    }
                }
            )
    before = deepcopy(content)
    displayed = present_meeting({"content": content})["content"]["occurrences"]
    for group in displayed:
        counts = {
            e["insight"]["provider"]: len(e["insight"]["actionItems"]) for e in group["insights"]
        }
        assert counts == {"openai": 10, "openrouter": 12}
        selected = select_session(content, group["id"])
        assert len(actions(selected, "openai")) == 10
        assert len(actions(selected, "openrouter")) == 12
    assert content == before


def test_custom_upload_keeps_only_ten_actions_in_both_mirrors():
    insight = {
        "id": "upload",
        "provider": "openai",
        "actionItems": [{"text": str(i)} for i in range(15)],
    }
    content = {"source": "upload", "insight": insight}
    projected = present_meeting({"content": content})["content"]
    assert len(projected["insight"]["actionItems"]) == 10
    assert len(projected["insights"][0]["insight"]["actionItems"]) == 10
    assert len(actions(select_session(content, None), "openai")) == 10
