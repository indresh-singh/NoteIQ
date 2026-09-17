import re
from datetime import datetime
from typing import Literal
from urllib.parse import quote, unquote
from uuid import UUID

from pydantic import BaseModel, Field


class Note(BaseModel):
    title: str | None = None
    text: str | None = None
    subpoints: list["Note"] = Field(default_factory=list)


class ActionItem(BaseModel):
    title: str | None = None
    text: str | None = None
    ownerDisplayName: str | None = None


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

    @classmethod
    def from_resource(cls, resource: str) -> "TranscriptEvent":
        # Graph uses OData parentheses in basic transcript notifications.
        match = re.fullmatch(
            r"/?users/([^/]+)/onlineMeetings\('([^']+)'\)/transcripts\('([^']+)'\)", resource
        ) or re.fullmatch(r"/?users/([^/]+)/onlineMeetings/([^/]+)/transcripts/([^/?#]+)", resource)
        if not match:
            raise ValueError("Unsupported transcript resource")
        user_id, meeting_id, transcript_id = map(unquote, match.groups())
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


class UserSync(BaseModel):
    type: Literal["user_sync"] = "user_sync"
    user_id: UUID


def parse_event(payload: str) -> InsightEvent | TranscriptEvent | MeetingSync | UserSync:
    import json

    data = json.loads(payload)
    if data.get("type") == "user_sync":
        return UserSync.model_validate(data)
    model = (
        TranscriptEvent
        if "transcript_id" in data
        else (InsightEvent if "insight_id" in data else MeetingSync)
    )
    return model.model_validate(data)
