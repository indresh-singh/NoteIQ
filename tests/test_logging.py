import json
import logging

import httpx

from app.graph_client import safe_graph_url
from app.observability import ContextFilter, log_context, response_diagnostics


def test_log_context_adds_request_and_job_correlation():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
    with log_context(request_id="request-123", job_id=42):
        assert ContextFilter().filter(record)
    assert record.request_id == "request-123"
    assert record.job_id == "42"


def test_upstream_diagnostics_redact_secrets_and_omit_query_strings():
    response = httpx.Response(
        400,
        headers={"request-id": "provider-123", "content-type": "application/json"},
        json={
            "error": {"code": "BadRequest", "message": "Useful provider detail"},
            "access_token": "never-log-this",
            "nested": {"client_secret": "never-log-this-either"},
        },
        request=httpx.Request("GET", "https://provider.test/path?code=oauth-secret"),
    )
    details = response_diagnostics(response)
    parsed = json.loads(details)
    assert parsed["url"] == "https://provider.test/path"
    assert parsed["headers"]["request-id"] == "provider-123"
    assert parsed["body"]["error"]["message"] == "Useful provider detail"
    assert "oauth-secret" not in details
    assert "never-log-this" not in details


def test_graph_urls_keep_operations_but_hash_resource_identifiers():
    value = safe_graph_url(
        "https://graph.microsoft.com/v1.0/users/private-user/onlineMeetings/"
        "private-meeting/transcripts/private-transcript/content?$select=id"
    )
    assert "/users/<user:" in value
    assert "/onlineMeetings/<meeting:" in value
    assert "/transcripts/<transcript:" in value
    assert value.endswith("/content")
    assert "private-user" not in value
    assert "private-meeting" not in value
    assert "private-transcript" not in value
    assert "$select" not in value
