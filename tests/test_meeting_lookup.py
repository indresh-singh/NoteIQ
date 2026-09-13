import pytest

from app.meetings import meeting_filter


def test_short_meeting_link_uses_numeric_lookup():
    assert meeting_filter("https://teams.microsoft.com/meet/338180822475417?p=test") == (
        "joinMeetingIdSettings/joinMeetingId eq '338180822475417'"
    )


def test_legacy_link_escapes_odata_quote():
    assert meeting_filter("https://teams.microsoft.com/l/meetup-join/a'b") == (
        "JoinWebUrl eq 'https://teams.microsoft.com/l/meetup-join/a''b'"
    )


def test_foreign_link_rejected():
    with pytest.raises(ValueError):
        meeting_filter("https://example.org/meet/123")
