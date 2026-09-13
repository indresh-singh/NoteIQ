from unittest.mock import AsyncMock

import httpx

from scripts.diagnose_meeting import diagnose, resolve_user_id
from tests.conftest import USER


async def test_resolve_user_id_uses_email_filter():
    graph = AsyncMock()
    graph.list.return_value = [{"id": USER}]

    assert await resolve_user_id(graph, "demo@contoso.com") == USER
    graph.list.assert_awaited_once_with("/users?$filter=userPrincipalName eq 'demo@contoso.com' or mail eq 'demo@contoso.com'")


async def test_diagnostic_continues_to_insights_when_transcript_access_fails(monkeypatch, capsys):
    graph = AsyncMock()
    meeting = {"id": "meeting/id", "participants": {"organizer": {"identity": {"user": {"id": USER}}}}}
    response = httpx.Response(403, json={"error": {"code": "Forbidden"}}, request=httpx.Request("GET", "https://graph.microsoft.com"))
    graph.list.side_effect = [
        [meeting],
        httpx.HTTPStatusError("denied", request=response.request, response=response),
        [{"id": "insight"}],
        [],
    ]
    graph.request.return_value = {"id": "insight", "meetingNotes": [{"title": "Private", "text": "Do not print this content"}]}
    monkeypatch.setattr("scripts.diagnose_meeting.GraphClient", lambda: graph)
    await diagnose(USER, "https://teams.microsoft.com/meet/338180822475417?p=test")
    output = capsys.readouterr().out
    assert "HTTP 403" in output
    assert "1 notes" in output
    assert "Do not print this content" not in output
    assert graph.request.call_args.args[1].endswith("meeting%2Fid/aiInsights/insight")
