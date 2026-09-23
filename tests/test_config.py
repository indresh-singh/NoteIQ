from app.config import settings


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
