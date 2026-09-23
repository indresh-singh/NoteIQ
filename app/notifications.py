import hmac
from collections.abc import Callable

from app.config import Settings
from app.models import InsightEvent, TranscriptEvent


class UnsupportedNotificationResource(ValueError):
    """A created notification whose resource is not an event we subscribe to."""

    def __init__(self, resource: object):
        super().__init__("Unsupported notification resource")
        self.resource_shape = notification_resource_shape(resource)


def notification_resource_shape(resource: object) -> str:
    """Describe a Graph resource's syntax without putting its identifiers in logs."""
    if not isinstance(resource, str):
        return f"type={type(resource).__name__}"

    path = resource.lstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    if path.startswith("copilot/users/"):
        prefix = "copilot/users"
    elif path.startswith("users/"):
        prefix = "users"
    else:
        prefix = "other"

    def style(name: str) -> str:
        if any(segment == name for segment in segments):
            return "path"
        if any(segment.startswith(f"{name}(") for segment in segments):
            return "parenthesized"
        return "absent"

    return (
        f"prefix={prefix} segments={len(segments)} "
        f"meeting={style('onlineMeetings')} insight={style('aiInsights')} "
        f"transcript={style('transcripts')} query={'yes' if '?' in resource else 'no'} "
        f"fragment={'yes' if '#' in resource else 'no'}"
    )


def validate_notifications(
    payload: object,
    config: Settings,
    lifecycle: bool = False,
    subscription_user: Callable[[str, str], str | None] | None = None,
) -> list:
    """Returns event JSON strings normally, or (lifecycleEvent, resource) pairs when lifecycle=True."""
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
        raise ValueError("Expected a notification collection")
    messages = []
    for item in payload["value"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid notification")
        state = item.get("clientState")
        if not isinstance(state, str) or not hmac.compare_digest(
            state.encode(), config.client_state.get_secret_value().encode()
        ):
            raise PermissionError("Invalid client state")
        tenant = item.get("tenantId")
        if tenant and str(tenant).lower() != str(config.tenant_id):
            raise PermissionError("Invalid tenant")
        if lifecycle:
            if item.get("lifecycleEvent") in {
                "reauthorizationRequired",
                "subscriptionRemoved",
                "missed",
            }:
                messages.append((item["lifecycleEvent"], item.get("resource", "")))
        elif item.get("changeType") == "created":
            resource = item.get("resource", "")
            if not isinstance(resource, str):
                raise ValueError("Invalid resource")
            path = resource.lstrip("/")
            if path.startswith("users/"):
                model = TranscriptEvent
            elif path.startswith("copilot/users/"):
                model = InsightEvent
            elif path.startswith("communications/onlineMeetings("):
                subscription_id = item.get("subscriptionId")
                user_id = (
                    subscription_user(subscription_id, "transcripts")
                    if subscription_user and isinstance(subscription_id, str)
                    else None
                )
                try:
                    event = TranscriptEvent.from_resource(resource, user_id)
                except ValueError as error:
                    raise UnsupportedNotificationResource(resource) from error
                messages.append(event.model_dump_json())
                continue
            else:
                raise UnsupportedNotificationResource(resource)
            try:
                event = model.from_resource(resource)
            except ValueError as error:
                raise UnsupportedNotificationResource(resource) from error
            messages.append(event.model_dump_json())
    return messages
