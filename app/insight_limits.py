"""Apply the same action limit to saved views, emails and task exports."""

MAX_OPENAI_ACTION_ITEMS = 10


def limit_openai_actions(content: dict) -> dict:
    entries = content.get("insights") or (
        [{"insight": content["insight"]}] if content.get("insight") else []
    )
    remaining = MAX_OPENAI_ACTION_ITEMS
    limited = []
    for entry in entries:
        insight = entry["insight"]
        if insight.get("provider") == "openai":
            actions = (insight.get("actionItems") or [])[:remaining]
            remaining -= len(actions)
            insight = {**insight, "actionItems": actions}
        limited.append({**entry, "insight": insight})
    result = {**content, "insights": limited}
    if content.get("insight"):
        result["insight"] = next(
            (
                e["insight"]
                for e in limited
                if e["insight"].get("id") == content["insight"].get("id")
            ),
            content["insight"],
        )
    return result
