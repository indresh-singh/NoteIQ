import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
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
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str | None = None
    # API keys issued to an Enterprise project use the normal OpenAI API
    # endpoint; ChatGPT's interactive web endpoint is never called here.
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6-luna"
    # A deliberately low application-side ceiling. This is per worker process;
    # keep the worker replica count low until the Enterprise project's limits
    # are known.
    openai_min_request_interval_seconds: float = 30
    # "web" and "worker" split the two halves into separate containers so the web
    # tier can scale out; "all" keeps both in one process, as a single replica.
    role: Literal["all", "web", "worker"] = "all"
    job_concurrency: int = 4
    subscription_concurrency: int = 15
    meeting_retention_days: float | None = None

    @property
    def runs_worker(self) -> bool:
        return self.role in {"all", "worker"}

    @property
    def redirect_uri(self) -> str:
        return self.public_url + "/auth/callback"

    @property
    def clickup_redirect_uri(self) -> str:
        return self.public_url + "/clickup/callback"

    @property
    def clickup_enabled(self) -> bool:
        return self.clickup_client_id is not None

    @property
    def openrouter_enabled(self) -> bool:
        return self.openrouter_api_key is not None

    @property
    def openai_enabled(self) -> bool:
        return self.openai_api_key is not None

    @property
    def external_ai_enabled(self) -> bool:
        return self.openai_enabled or self.openrouter_enabled

    @property
    def summary_provider(self) -> str:
        """Configured transcript-summary service; OpenAI takes precedence."""
        if self.openai_enabled:
            return "openai"
        if self.openrouter_enabled:
            return "openrouter"
        return "copilot"


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
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
        openrouter_model=os.getenv("OPENROUTER_MODEL") or None,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
        openai_min_request_interval_seconds=os.getenv("OPENAI_MIN_REQUEST_INTERVAL_SECONDS", "30"),
        role=os.getenv("NOTEIQ_ROLE", "all"),
        job_concurrency=os.getenv("NOTEIQ_JOB_CONCURRENCY", "4"),
        subscription_concurrency=os.getenv("NOTEIQ_SUBSCRIPTION_CONCURRENCY", "15"),
        meeting_retention_days=os.getenv("NOTEIQ_MEETING_RETENTION_DAYS") or None,
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
    if not 1 <= config.job_concurrency <= 32:
        raise ValueError("NOTEIQ_JOB_CONCURRENCY must be between 1 and 32")
    if not 1 <= config.subscription_concurrency <= 64:
        raise ValueError("NOTEIQ_SUBSCRIPTION_CONCURRENCY must be between 1 and 64")
    if config.meeting_retention_days is not None and config.meeting_retention_days < 7:
        # Below the seven days discovery itself looks back, retention would
        # delete meetings the next sweep immediately re-fetches.
        raise ValueError("NOTEIQ_MEETING_RETENTION_DAYS must be at least 7")
    if bool(config.openrouter_api_key) != bool(config.openrouter_model):
        raise ValueError("Configure OPENROUTER_API_KEY and OPENROUTER_MODEL together")
    if not 1 <= config.openai_min_request_interval_seconds <= 3600:
        raise ValueError("OPENAI_MIN_REQUEST_INTERVAL_SECONDS must be between 1 and 3600")
    return config
