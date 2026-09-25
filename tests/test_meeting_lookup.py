import pytest

from app.meetings import meeting_filter, meeting_participants


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


def test_participants_list_organizer_and_attendees_once():
    meeting = {
        "participants": {
            "organizer": {
                "upn": "olivia@contoso.com",
                "identity": {"user": {"id": "1", "displayName": "Olivia"}},
            },
            "attendees": [
                {"upn": "sam@contoso.com", "identity": {"user": {"displayName": "Sam"}}},
                {"upn": "SAM@contoso.com", "identity": {"user": {"displayName": "Sam"}}},
                {"upn": "guest@fabrikam.com", "identity": {"guest": {"displayName": None}}},
                {"identity": {}},
            ],
        }
    }
    assert meeting_participants(meeting) == [
        {"name": "Olivia", "email": "olivia@contoso.com", "organizer": True},
        {"name": "Sam", "email": "sam@contoso.com", "organizer": False},
        {"name": None, "email": "guest@fabrikam.com", "organizer": False},
    ]


def test_missing_participants_do_not_overwrite_saved_list():
    assert meeting_participants({}) is None
    assert meeting_participants({"participants": {"organizer": {"identity": {}}}}) is None
