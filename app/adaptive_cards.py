import json
from datetime import timezone

from microsoft_teams.cards import AdaptiveCard

from app.models import Insight, Note


def note_lines(note: Note, depth: int = 0) -> list[str]:
    content = ": ".join(part.strip() for part in (note.title, note.text) if part and part.strip())
    lines = [f"{'  ' * depth}• {content}"] if content else []
    for child in note.subpoints:
        lines.extend(note_lines(child, depth + 1))
    return lines


def build_card(
    insight: Insight, subject: str, *, source: str = "Microsoft 365 Copilot meeting insights"
) -> dict | None:
    notes = [line for note in insight.meetingNotes for line in note_lines(note)]
    actions = []
    for action in insight.actionItems:
        content = ": ".join(
            part.strip() for part in (action.title, action.text) if part and part.strip()
        )
        if content:
            owner = (action.ownerDisplayName or "").strip() or "Owner not specified"
            line = f"• **{owner}** — {content}"
            due = (action.dueDate or "").strip()
            if due:
                line += f" (Due: **{due}**)"
            actions.append(line)
    if not notes and not actions:
        return None

    def block(text: str, **style: object) -> dict:
        return {"type": "TextBlock", "text": text, "wrap": True, **style}

    body = [
        block("MEETING AI INTELLIGENCE", weight="Bolder", size="Large"),
        block(subject or "Teams meeting", weight="Bolder"),
    ]
    for title, lines in (("KEY NOTES", notes), ("ACTION ITEMS", actions)):
        if lines:
            body.append(block(title, weight="Bolder", separator=True))
            body.extend(block(line) for line in lines)
    if insight.endDateTime:
        ended = insight.endDateTime.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        body.append(block(f"Meeting ended: {ended}", isSubtle=True))
    body.append(block(f"Generated from {source}.", isSubtle=True))
    card = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
    }
    # Keep margin for the activity envelope under Teams' message size limit.
    if len(json.dumps(card, ensure_ascii=False).encode()) > 24_000:
        raise ValueError("Card exceeds pilot size limit; manual review required")
    AdaptiveCard.model_validate(card)
    return card
