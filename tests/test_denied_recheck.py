"""A denied re-check must not put an access alarm above content the person can already read."""

from app.models import MeetingSync
from app.sync import ACCESS_DENIED_MESSAGE, DENIED_MESSAGES, queue_sync, sync_meeting
from tests.conftest import USER
from tests.test_sync import graph_error

INSIGHTS_DENIED = DENIED_MESSAGES[frozenset({"insight"})]


def complete(provider=None):
    insight = {"id": "i", **({"provider": provider} if provider else {})}
    return {
        "meeting_id": "m",
        "transcripts": [{"transcript": {"id": "t"}}],
        "insights": [{"insight": insight}],
    }


def lists(*, transcripts=None, insights=None):
    """graph.list side effect: an exception to raise, or items to return, per kind."""

    async def answer(path):
        result = insights if path.endswith("/aiInsights") else transcripts
        if isinstance(result, Exception):
            raise result
        return result or []

    return answer


async def check(store, graph):
    return await sync_meeting(MeetingSync(user_id=USER, meeting_id="m"), graph, store)


async def test_denied_recheck_of_a_complete_meeting_shows_no_warning(store, graph):
    """The reported incident: yesterday's finished meeting, re-checked by Refresh."""
    store.save_meeting(USER, "Yesterday", complete())
    graph.request.side_effect = graph_error(403)
    assert await check(store, graph) == "SKIPPED_ACCESS_DENIED"
    saved = store.find_meeting(USER, "m")
    assert "sync_message" not in saved
    # Still terminal, so the sweep does not ask Graph again every minute.
    assert saved["sync_status"] == "SKIPPED_ACCESS_DENIED"
    assert queue_sync(store, USER) == 0


async def test_a_warning_left_by_an_earlier_recheck_is_cleared(store, graph):
    store.save_meeting(
        USER,
        "Yesterday",
        {
            **complete(),
            "sync_status": "SKIPPED_ACCESS_DENIED",
            "sync_message": ACCESS_DENIED_MESSAGE,
        },
    )
    graph.list.side_effect = lists(insights=graph_error(403))
    await check(store, graph)
    assert "sync_message" not in store.find_meeting(USER, "m")


async def test_an_external_provider_summary_counts_as_insights(store, graph):
    """The second card in the report: an OpenRouter summary, Copilot insights denied."""
    store.save_meeting(USER, "OpenRouter", complete(provider="openrouter"))
    graph.list.side_effect = lists(insights=graph_error(403))
    await check(store, graph)
    assert "sync_message" not in store.find_meeting(USER, "m")


async def test_the_warning_names_only_what_is_missing(store, graph):
    store.save_meeting(
        USER, "Transcript only", {"meeting_id": "m", "transcripts": [{"transcript": {"id": "t"}}]}
    )
    graph.list.side_effect = lists(transcripts=graph_error(403), insights=graph_error(403))
    assert await check(store, graph) == "SKIPPED_ACCESS_DENIED"
    assert store.find_meeting(USER, "m")["sync_message"] == INSIGHTS_DENIED


async def test_a_meeting_with_nothing_still_explains_the_denial(store, graph):
    store.save_meeting(USER, "Nothing yet", {"meeting_id": "m"})
    graph.request.side_effect = graph_error(403)
    await check(store, graph)
    assert store.find_meeting(USER, "m")["sync_message"] == ACCESS_DENIED_MESSAGE


async def test_a_rejected_recheck_of_a_complete_meeting_shows_no_warning(store, graph):
    store.save_meeting(USER, "Yesterday", complete())
    graph.request.side_effect = graph_error(404, "ItemNotFound")
    assert await check(store, graph) == "SKIPPED_GRAPH_REJECTED"
    assert "sync_message" not in store.find_meeting(USER, "m")


async def test_the_log_says_which_call_was_blocked(store, graph, caplog):
    store.save_meeting(USER, "Yesterday", complete())
    graph.list.side_effect = lists(insights=graph_error(403))
    await check(store, graph)
    assert "blocked_kinds=insight held_kinds=insight,transcript warning_shown=False" in caplog.text
