import pytest

from app.config import ROOT, app_environment, environment_file, environment_value, settings


@pytest.mark.parametrize(
    ("value", "selected", "filename"),
    [
        ("dev", "dev", ".env.dev"),
        ("stg", "stg", ".env.dev"),
        ("prod", "prod", ".env.prod"),
        ("PROD", "prod", ".env.prod"),
    ],
)
def test_app_env_selects_one_of_two_environment_files(monkeypatch, value, selected, filename):
    monkeypatch.setenv("APP_ENV", value)
    assert app_environment() == selected
    assert environment_file(app_environment()) == ROOT / filename


def test_invalid_app_env_is_rejected(monkeypatch):
    monkeypatch.setenv("APP_ENV", "qa")
    settings.cache_clear()
    with pytest.raises(ValueError, match="APP_ENV must be one of"):
        settings()


def test_deployment_alias_supplies_runtime_setting(monkeypatch):
    monkeypatch.delenv("GRAPH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("NOTEIQ_DEPLOY_GRAPH_CLIENT_SECRET", "deployment-secret")

    assert (
        environment_value("GRAPH_CLIENT_SECRET", "NOTEIQ_DEPLOY_GRAPH_CLIENT_SECRET")
        == "deployment-secret"
    )


def test_runtime_setting_takes_precedence_over_deployment_alias(monkeypatch):
    monkeypatch.setenv("GRAPH_CLIENT_SECRET", "runtime-secret")
    monkeypatch.setenv("NOTEIQ_DEPLOY_GRAPH_CLIENT_SECRET", "deployment-secret")

    assert (
        environment_value("GRAPH_CLIENT_SECRET", "NOTEIQ_DEPLOY_GRAPH_CLIENT_SECRET")
        == "runtime-secret"
    )


def test_settings_loads_shared_dev_file_for_staging(monkeypatch):
    loaded = []
    monkeypatch.setenv("APP_ENV", "stg")
    monkeypatch.setattr(
        "app.config.load_dotenv", lambda path, **kwargs: loaded.append((path, kwargs))
    )
    settings.cache_clear()

    config = settings()

    assert config.app_env == "stg"
    assert loaded == [(ROOT / ".env.dev", {"interpolate": False})]


def test_no_external_summary_service_is_configured_by_default(config):
    assert config.summary_provider == "copilot"
    assert config.summary_providers == ("copilot",)
    assert config.openrouter_enabled is False


def test_openrouter_is_used_when_its_credentials_are_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()
    config = settings()
    assert config.summary_provider == "openrouter"
    assert config.summary_providers == ("copilot", "openrouter")
    assert config.openrouter_enabled is True


def test_openai_takes_precedence_when_its_key_is_configured(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-enterprise-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "test/model")
    settings.cache_clear()
    config = settings()
    assert config.openai_enabled is True
    assert config.external_ai_enabled is True
    assert config.summary_provider == "openai"
    assert config.summary_providers == ("copilot", "openai", "openrouter")
    assert config.openai_model == "gpt-5.6-luna"
