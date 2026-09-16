import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from dotenv import load_dotenv
from pydantic import BaseModel, SecretStr

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseModel):
    tenant_id: UUID
    graph_client_id: UUID
    graph_secret: SecretStr
    client_state: SecretStr
    public_url: str
    database: Path = ROOT / "data/noteiq.sqlite3"
    backup_database: Path | None = None
    database_url: SecretStr | None = None
    teams_app_id: UUID | None = None
    clickup_client_id: str | None = None
    clickup_client_secret: SecretStr | None = None
    clickup_token_key: SecretStr | None = None

    @property
    def redirect_uri(self) -> str:
        return self.public_url + "/auth/callback"

    @property
    def clickup_redirect_uri(self) -> str:
        return self.public_url + "/clickup/callback"

    @property
    def clickup_enabled(self) -> bool:
        return self.clickup_client_id is not None


@lru_cache
def settings() -> Settings:
    load_dotenv(ROOT / ".env", interpolate=False)
    config = Settings(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        graph_client_id=os.environ["GRAPH_CLIENT_ID"],
        graph_secret=os.environ["GRAPH_CLIENT_SECRET"],
        client_state=os.environ["GRAPH_CLIENT_STATE"],
        public_url=os.environ["PUBLIC_BASE_URL"].rstrip("/"),
        database=os.getenv("NOTEIQ_DATABASE", str(ROOT / "data/noteiq.sqlite3")),
        backup_database=os.getenv("NOTEIQ_BACKUP_DATABASE") or None,
        database_url=os.getenv("NOTEIQ_DATABASE_URL") or None,
        teams_app_id=os.getenv("TEAMS_APP_ID") or None,
        clickup_client_id=os.getenv("CLICKUP_CLIENT_ID") or None,
        clickup_client_secret=os.getenv("CLICKUP_CLIENT_SECRET") or None,
        clickup_token_key=os.getenv("CLICKUP_TOKEN_KEY") or None,
    )
    url = urlsplit(config.public_url)
    if url.scheme != "https" or not url.hostname or url.path or url.query or url.fragment:
        raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin, without a path")
    if url.username or url.password:
        raise ValueError("PUBLIC_BASE_URL cannot contain credentials")
    if not 32 <= len(config.client_state.get_secret_value()) <= 128:
        raise ValueError("GRAPH_CLIENT_STATE must contain 32–128 characters")
    if not config.graph_secret.get_secret_value():
        raise ValueError("GRAPH_CLIENT_SECRET is required")
    if config.database_url and not config.database_url.get_secret_value().startswith(
        ("postgresql://", "postgres://")
    ):
        raise ValueError("NOTEIQ_DATABASE_URL must be a PostgreSQL connection string")
    clickup_values = (
        config.clickup_client_id,
        config.clickup_client_secret,
        config.clickup_token_key,
    )
    if any(clickup_values) and not all(clickup_values):
        raise ValueError(
            "Configure CLICKUP_CLIENT_ID, CLICKUP_CLIENT_SECRET and CLICKUP_TOKEN_KEY together"
        )
    return config
