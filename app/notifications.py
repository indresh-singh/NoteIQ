import hmac

from app.config import Settings
from app.models import InsightEvent, TranscriptEvent


def validate_notifications(payload: object, config: Settings, lifecycle: bool = False) -> list:
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
            model = TranscriptEvent if resource.lstrip("/").startswith("users/") else InsightEvent
            event = model.from_resource(resource)
            messages.append(event.model_dump_json())
    return messages
