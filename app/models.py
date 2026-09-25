import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote, unquote
from uuid import UUID

from pydantic import BaseModel, Field


def age_seconds(value: "datetime | str | None") -> float | None:
    """Seconds since an instant, for latency logging. None when unparseable.

    Graph timestamps arrive as ISO-8601 with a trailing Z; treat a naive value
    as UTC so a missing offset cannot silently skew a measurement.
    """
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - value).total_seconds()


class Note(BaseModel):
    title: str | None = None
    text: str | None = None
    subpoints: list["Note"] = Field(default_factory=list)


class ActionItem(BaseModel):
    title: str | None = None
    text: str | None = None
    ownerDisplayName: str | None = None
    dueDate: str | None = None


class Insight(BaseModel):
    id: str
    contentCorrelationId: str | None = None
    createdDateTime: datetime | None = None
    endDateTime: datetime | None = None
    meetingNotes: list[Note] = Field(default_factory=list)
    actionItems: list[ActionItem] = Field(default_factory=list)


class InsightEvent(BaseModel):
    user_id: UUID
    meeting_id: str = Field(min_length=1)
    insight_id: str = Field(min_length=1)

    @classmethod
    def from_resource(cls, resource: str) -> "InsightEvent":
        match = re.fullmatch(
            r"/?copilot/users/([^/]+)/onlineMeetings/([^/]+)/aiInsights/([^/?#]+)",
            resource,
        )
        if not match:
            raise ValueError("Unsupported insight resource")
        user_id, meeting_id, insight_id = map(unquote, match.groups())
        if any(value in {".", ".."} for value in (meeting_id, insight_id)):
            raise ValueError("Invalid resource identifier")
        return cls(user_id=user_id, meeting_id=meeting_id, insight_id=insight_id)

    @property
    def meeting_path(self) -> str:
        return f"/users/{self.user_id}/onlineMeetings/{quote(self.meeting_id, safe='')}"

    @property
    def insight_path(self) -> str:
        return f"/copilot{self.meeting_path}/aiInsights/{quote(self.insight_id, safe='')}"


class TranscriptEvent(BaseModel):
    user_id: UUID
    meeting_id: str = Field(min_length=1)
    transcript_id: str = Field(min_length=1)
    metadata_only: bool = False

    @classmethod
    def from_resource(
        cls, resource: str, subscription_user_id: str | None = None
    ) -> "TranscriptEvent":
        # Graph uses OData parentheses in basic transcript notifications.
        match = re.fullmatch(
            r"/?users/([^/]+)/onlineMeetings\('([^']+)'\)/transcripts\('([^']+)'\)", resource
        ) or re.fullmatch(r"/?users/([^/]+)/onlineMeetings/([^/]+)/transcripts/([^/?#]+)", resource)
        if match:
            user_id, meeting_id, transcript_id = map(unquote, match.groups())
        else:
            # getAllTranscripts notifications use a canonical communications
            # resource that omits the subscribed user. The caller recovers
            # that user from the notification's subscriptionId.
            match = re.fullmatch(
                r"/?communications/onlineMeetings\('([^']+)'\)/transcripts\('([^']+)'\)",
                resource,
            )
            if not match or not subscription_user_id:
                raise ValueError("Unsupported transcript resource")
            user_id = subscription_user_id
            meeting_id, transcript_id = map(unquote, match.groups())
        if any(value in {".", ".."} for value in (meeting_id, transcript_id)):
            raise ValueError("Invalid resource identifier")
        return cls(user_id=user_id, meeting_id=meeting_id, transcript_id=transcript_id)

    @property
    def meeting_path(self) -> str:
        return f"/users/{self.user_id}/onlineMeetings/{quote(self.meeting_id, safe='')}"

    @property
    def transcript_path(self) -> str:
        return f"{self.meeting_path}/transcripts/{quote(self.transcript_id, safe='')}"


class MeetingSync(BaseModel):
    user_id: UUID
    meeting_id: str = Field(min_length=1)


class SessionInsightEvent(BaseModel):
    type: Literal["session_insight"] = "session_insight"
    user_id: UUID
    meeting_id: str = Field(min_length=1)
    occurrence_id: str = Field(min_length=1)
    provider: Literal["openai", "openrouter"]


class UserSync(BaseModel):
    type: Literal["user_sync"] = "user_sync"
    user_id: UUID


def parse_event(
    payload: str,
) -> InsightEvent | TranscriptEvent | MeetingSync | SessionInsightEvent | UserSync:
    import json

    data = json.loads(payload)
    if data.get("type") == "user_sync":
        return UserSync.model_validate(data)
    if data.get("type") == "session_insight":
        return SessionInsightEvent.model_validate(data)
    model = (
        TranscriptEvent
        if "transcript_id" in data
        else (InsightEvent if "insight_id" in data else MeetingSync)
    )
    return model.model_validate(data)
