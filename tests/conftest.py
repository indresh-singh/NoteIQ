import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.auth import graph_identity
from app.config import settings
from app.store import Store
from app.web import create_app

USER = "11111111-1111-1111-1111-111111111111"
TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SECRET = "sample-client-state-for-local-tests-only"


@pytest.fixture(autouse=True)
def config(monkeypatch, tmp_path):
    values = {
        "AZURE_TENANT_ID": TENANT,
        "GRAPH_CLIENT_ID": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "GRAPH_CLIENT_SECRET": "test-only-secret",
        "GRAPH_CLIENT_STATE": SECRET,
        "PUBLIC_BASE_URL": "https://noteiq.test",
        "NOTEIQ_DATABASE": str(tmp_path / "noteiq.sqlite3"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("app.config.load_dotenv", lambda *args, **kwargs: None)
    settings.cache_clear()
    graph_identity.cache_clear()
    yield settings()
    settings.cache_clear()
    graph_identity.cache_clear()


@pytest.fixture
def store(config):
    result = Store(config.database)
    result.enroll(USER, "Demo organizer")
    return result


@pytest.fixture
def graph():
    result = AsyncMock()
    result.list.return_value = []
    return result


@pytest.fixture
def client(config, graph):
    with TestClient(
        create_app(config, graph, background=False), base_url=config.public_url
    ) as result:
        yield result


@pytest.fixture
def signed_in(client, store):
    return {"Authorization": "Bearer " + store.session(USER)}


@pytest.fixture
def samples():
    root = Path(__file__).parent / "fixtures"
    return {
        name: json.loads((root / f"{name}.json").read_text())
        for name in ("notification", "insight", "meeting")
    }
